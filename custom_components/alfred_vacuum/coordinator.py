"""Aggiornamento dello stato, stanze, mappa e sessioni di pulizia."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .cloud import CloudAuthError, CloudError
from .const import (
    A_START,
    CONF_LIVE_MAP_INTERVAL,
    CONF_MAP_INTERVAL,
    CONF_ROOM_NAMES,
    CONF_SCAN_INTERVAL,
    DEFAULT_LIVE_MAP_INTERVAL,
    DEFAULT_MAP_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_CLEANING_FINISHED,
    EVENT_ROOMS_CHANGED,
    LAST_CLEAN_IMAGE,
    MEDIA_SUBDIR,
    P_BATTERY,
    P_CLEANING_AREA,
    P_CLEANING_TIME,
    P_STATUS,
)
from .device import AlfredRobot, RobotError
from .history import RoomHistory
from .map_decoder import MapData, Room, describe
from .map_render import pixel_histogram, render

_LOGGER = logging.getLogger(__name__)

# Aggiornamenti falliti di fila tollerati prima di rendere le entità non disponibili.
# Con l'intervallo predefinito (15 s) sono circa 1 minuto e mezzo: un singhiozzo del
# Wi‑Fi o del cloud Xiaomi non fa sparire il robot da automazioni e Node-RED.
MAX_MISSED_UPDATES = 6

CLEANING_STATUSES = {1, 7, 12}
HOME_STATUSES = {2, 6, 13}  # in attesa, in carica, carica completata
DOCKED_STATUSES = {6, 13}


def parse_room_overrides(text: str | None) -> dict[int | str, str]:
    """'1=Soggiorno, Camera da letto 2=Cameretta' -> {1: 'Soggiorno', 'camera da letto 2': 'Cameretta'}.

    La chiave può essere il numero (cambia se rifai la mappatura) oppure il
    nome letto dalla mappa (resta valido anche dopo una nuova mappatura).
    """
    result: dict[int | str, str] = {}
    for part in (text or "").replace("\n", ",").split(","):
        if "=" not in part:
            continue
        key, _, name = part.partition("=")
        key, name = key.strip(), name.strip()
        if not key or not name:
            continue
        result[int(key) if key.isdigit() else key.casefold()] = name
    return result


def parse_names(value: str | list[str] | None) -> list[str]:
    """Nomi da un elenco o da un testo separato da virgole."""
    if isinstance(value, list):
        return [str(n).strip() for n in value if str(n).strip()]
    return [n.strip() for n in (value or "").replace("\n", ",").split(",") if n.strip()]


@dataclass
class CleaningSession:
    started: datetime
    source: str
    requested: list[str] | None  # nomi richiesti (None = tutta la casa o sconosciuto)
    full: bool = False
    zones: list[list[float]] | None = None  # rettangoli della pulizia a zona
    visited: Counter = field(default_factory=Counter)
    max_area: int = 0
    max_time: int = 0


class AlfredCoordinator(DataUpdateCoordinator[dict[tuple[int, int], Any]]):
    """Legge lo stato del robot e, quando serve, la mappa."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, robot: AlfredRobot) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
        )
        self.robot = robot
        self.entry = entry
        self.history = RoomHistory(hass, entry.entry_id)
        self.map: MapData | None = None
        self.map_details: dict[str, Any] = {}
        self.map_updated: datetime | None = None
        self.map_error: str | None = None
        self.map_image: bytes | None = None
        self.map_attributes: dict[str, Any] = {}
        self.selected_room: str | None = None
        self.session: CleaningSession | None = None
        self.flags: dict[str, bool] = {}  # interruttori delle automazioni
        self._pending: tuple[str, list[str] | None, bool, list[list[float]] | None] | None = None
        self._force_map = False
        self._last_status: Any = None
        self._map_attempts = 0
        self._map_busy = False
        self._missed_updates = 0

    async def async_setup(self) -> None:
        await self.history.async_load()

    @property
    def last_session(self) -> dict[str, Any] | None:
        return self.history.last_session

    # --- stanze ----------------------------------------------------------
    @property
    def overrides(self) -> dict[int | str, str]:
        return parse_room_overrides(self.entry.options.get(CONF_ROOM_NAMES))

    @property
    def rooms(self) -> dict[int, Room]:
        """Stanze lette dalla mappa, con i nomi scelti nelle opzioni."""
        rooms: dict[int, Room] = {}
        if self.map:
            rooms = {k: Room(v.room_id, v.name, v.room_type, v.custom_name) for k, v in self.map.rooms.items()}
        overrides = self.overrides
        for room in rooms.values():
            new = overrides.get(room.room_id) or overrides.get(room.name.casefold())
            if new:
                room.name = new
        for key, name in overrides.items():
            if isinstance(key, int) and key not in rooms and not self.map:
                rooms[key] = Room(key, name)
        return dict(sorted(rooms.items()))

    @property
    def room_names(self) -> list[str]:
        return [r.name for r in self.rooms.values()]

    def resolve_room(self, value: str | int) -> int:
        """Accetta un nome ('cucina') o un numero e restituisce il numero ATTUALE."""
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        wanted = text.casefold()
        for room in self.rooms.values():
            if room.name.casefold() == wanted:
                return room.room_id
        for room in self.rooms.values():
            if wanted in room.name.casefold():
                return room.room_id
        raise ValueError(f"Stanza '{value}' non trovata. Stanze note: {', '.join(self.room_names) or 'nessuna'}")

    def room_name(self, room_id: int) -> str:
        room = self.rooms.get(room_id)
        return room.name if room else f"Stanza {room_id}"

    @property
    def current_room(self) -> Room | None:
        if not self.map:
            return None
        room_id = self.map.robot_room
        return self.rooms.get(room_id) if room_id is not None else None

    # --- comandi che aprono una sessione ---------------------------------
    async def async_clean_rooms(
        self, rooms: list[str | int], source: str = "manuale", repeats: int = 1,
        fan: int | None = None, water: int | None = None,
    ) -> list[str]:
        """Pulisce le stanze indicate per nome o numero. Restituisce i nomi."""
        ids = [self.resolve_room(r) for r in rooms]
        names = [self.room_name(i) for i in ids]
        await self.robot.clean_rooms(ids, self.data or {}, repeats=repeats, fan=fan, water=water)
        if self.session:  # nuova richiesta durante una pulizia: aggiorno la sessione in corso
            self.session.requested, self.session.source, self.session.zones = names, source, None
            self._force_map = True
        else:
            self._pending = (source, names, False, None)
        await self.async_request_refresh()
        return names

    async def async_clean_zones(
        self, zones: list[list[float]], source: str = "zona", repeats: int = 1,
        fan: int | None = None, water: int | None = None,
    ) -> None:
        """Pulisce dei rettangoli disegnati sulla mappa (coordinate del robot)."""
        await self.robot.clean_zones(zones, self.data or {}, repeats=repeats, fan=fan, water=water)
        zones = [[float(v) for v in zone[:4]] for zone in zones]
        if self.session:
            self.session.requested, self.session.zones, self.session.source = None, zones, source
            self._force_map = True
        else:
            self._pending = (source, None, False, zones)
        await self.async_request_refresh()

    async def async_clean_all(self, source: str = "manuale") -> None:
        await self.robot.action(A_START)
        if self.session is None:  # se era in pausa riprende la sessione esistente
            self._pending = (source, None, True, None)
        await self.async_request_refresh()

    def note_external_start(self, source: str, full: bool) -> None:
        """Avvio fatto da un'entità (es. pulsante Avvia): serve per lo storico."""
        if self.session is None:
            self._pending = (source, None, full, None)

    # --- mappa -----------------------------------------------------------
    async def async_refresh_map(self) -> dict[str, Any]:
        """Rilegge la mappa dal robot. Restituisce un riassunto."""
        try:
            data, details = await self.robot.request_map()
        except (RobotError, CloudError) as err:
            self.map_error = str(err)
            self.map_details = {"error": str(err)}
            return self.map_details
        self.map_details = details
        if data is None:
            self.map_error = details.get("error")
            self.async_update_listeners()
            return details
        self.map = data
        self.map_updated = dt_util.utcnow()
        self.map_error = None
        self._check_rooms_changed()
        if self.session:
            room = data.robot_room
            if room is not None:
                self.session.visited[room] += 1
        await self._render()
        details = {**details, **describe(data), "pixel_values": pixel_histogram(data)}
        self.map_details = details
        self.async_update_listeners()
        return details

    async def _render(self) -> None:
        if not self.map:
            return
        highlight: set[int] | None = None
        zones: list[list[float]] | None = None
        if self.session:
            if self.session.requested:
                highlight = {self.resolve_room(n) for n in self.session.requested if n in self.room_names}
            zones = self.session.zones
        try:
            self.map_image, self.map_attributes = await self.hass.async_add_executor_job(
                render, self.map, self.rooms, highlight, zones
            )
        except Exception:  # noqa: BLE001 - un errore di disegno non deve fermare il resto
            _LOGGER.exception("Disegno della mappa non riuscito")

    def _check_rooms_changed(self) -> None:
        current = {r.room_id: r.name for r in self.rooms.values()}
        diff = self.history.compare(current)
        self.history.remember_rooms(current)
        if not diff:
            return
        table = "\n".join(f"- {k} → **{v}**" for k, v in diff["dopo"].items())
        lines = [
            "Il robot ha una nuova mappa. Le automazioni che usano i **nomi** continuano a funzionare.",
            "",
            "Nuova tabella numero → stanza:",
            table,
        ]
        if diff["nomi_spariti"]:
            lines += ["", "⚠️ Nomi non più presenti: " + ", ".join(diff["nomi_spariti"])
                      + ". Dai il nome o il tipo giusto alle stanze nell'app, poi premi 'Rileggi mappa'."]
        persistent_notification.async_create(
            self.hass, "\n".join(lines), title="ALFRED: stanze cambiate", notification_id=f"{DOMAIN}_rooms_changed"
        )
        self.hass.bus.async_fire(EVENT_ROOMS_CHANGED, diff)

    # --- stato -----------------------------------------------------------
    async def _async_update_data(self) -> dict[tuple[int, int], Any]:
        try:
            data = await self.robot.get_properties()
        except CloudAuthError as err:
            # se il robot risponde in locale, una sessione cloud scaduta non lo blocca:
            # qui arriva solo quando anche il locale è fallito
            raise ConfigEntryAuthFailed("Sessione Xiaomi scaduta, rifai il login") from err
        except Exception as err:  # noqa: BLE001 - qualsiasi errore di rete passa dalla tolleranza
            return self._tolerate_failure(err)

        if self._missed_updates:
            _LOGGER.info("Robot di nuovo raggiungibile dopo %s aggiornamenti falliti", self._missed_updates)
            self._missed_updates = 0

        status = data.get(P_STATUS)
        previous = self._last_status
        self._last_status = status
        now = dt_util.utcnow()

        # inizio sessione
        if status in CLEANING_STATUSES and self.session is None:
            source, names, full, zones = self._pending or ("esterno", None, False, None)
            self.session = CleaningSession(started=now, source=source, requested=names, full=full, zones=zones)
            self._pending = None
            self._force_map = True  # subito la mappa con le stanze/zone evidenziate
        if self.session:
            self.session.max_area = max(self.session.max_area, int(data.get(P_CLEANING_AREA) or 0))
            self.session.max_time = max(self.session.max_time, int(data.get(P_CLEANING_TIME) or 0))

        # fine sessione: robot di nuovo a casa
        if self.session and status in HOME_STATUSES and previous not in HOME_STATUSES:
            self.hass.async_create_task(self._finish_session())
        elif not self._map_busy:
            if self.session and status in CLEANING_STATUSES:
                live = self.entry.options.get(CONF_LIVE_MAP_INTERVAL, DEFAULT_LIVE_MAP_INTERVAL)
                due = live and (self.map_updated is None or now - self.map_updated >= timedelta(seconds=live))
                if due or self._force_map:
                    self._force_map = False
                    self.hass.async_create_task(self._background_map())
            else:
                interval = timedelta(minutes=self.entry.options.get(CONF_MAP_INTERVAL, DEFAULT_MAP_INTERVAL))
                stale = self.map_updated is None or now - self.map_updated > interval
                if stale and self._map_attempt_due():
                    self.hass.async_create_task(self._background_map())
        return data

    def _tolerate_failure(self, err: Exception) -> dict[tuple[int, int], Any]:
        """Tiene gli ultimi valori per qualche aggiornamento prima di arrendersi."""
        self._missed_updates += 1
        reason = str(err) or type(err).__name__
        if not isinstance(err, (RobotError, CloudError)):
            _LOGGER.debug("Errore inatteso leggendo lo stato del robot", exc_info=err)
        if self.data is None or self._missed_updates >= MAX_MISSED_UPDATES:
            # primo avvio senza dati, oppure guasto che dura: entità non disponibili
            raise UpdateFailed(f"Robot non raggiungibile ({self._missed_updates} tentativi): {reason}") from err
        if self._missed_updates == 1:
            _LOGGER.warning("Lettura dello stato non riuscita, tengo gli ultimi valori: %s", reason)
        return self.data

    def _map_attempt_due(self) -> bool:
        """Senza errori legge subito; dopo un errore ritenta ogni 40 aggiornamenti."""
        if not self.map_error:
            return True
        self._map_attempts += 1
        return self._map_attempts % 40 == 0

    async def _background_map(self) -> None:
        self._map_busy = True
        try:
            await self.async_refresh_map()
        except Exception:  # noqa: BLE001 - la mappa non deve mai bloccare lo stato
            _LOGGER.exception("Lettura della mappa non riuscita")
        finally:
            self._map_busy = False

    # --- fine pulizia ----------------------------------------------------
    def _cleaned_rooms(self, session: CleaningSession) -> list[str]:
        if session.requested:
            return list(session.requested)
        visited = [room for room, count in session.visited.items() if count >= 2 and room in self.rooms]
        if visited:
            return [self.room_name(r) for r in sorted(visited)]
        if session.full:
            return self.room_names
        return []

    async def _finish_session(self) -> None:
        session = self.session
        self.session = None
        if session is None:
            return
        self._map_busy = True
        try:
            await self.async_refresh_map()  # mappa finale (anche per la foto)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Mappa finale non letta", exc_info=True)
        finally:
            self._map_busy = False
        rooms = self._cleaned_rooms(session)
        finished = dt_util.utcnow()
        if rooms and session.max_area > 0:
            self.history.mark_cleaned(rooms, finished)
        image_path = await self._save_image(await self._render_session(session, rooms))
        self.history.last_session = {
            "stanze": rooms,
            "area_m2": session.max_area,
            "durata_min": session.max_time,
            "avviata_da": session.source,
            "inizio": session.started.isoformat(),
            "fine": finished.isoformat(),
            "immagine": image_path,
            "batteria": (self.data or {}).get(P_BATTERY),
        }
        self.history.save()
        self.hass.bus.async_fire(EVENT_CLEANING_FINISHED, {"entry_id": self.entry.entry_id, **self.history.last_session})
        self.async_update_listeners()

    async def _render_session(self, session: CleaningSession, rooms: list[str]) -> bytes | None:
        """Immagine di fine pulizia (Telegram): stanze pulite o zone evidenziate.

        La mappa in dashboard torna normale; questa immagine si disegna a parte
        perché a questo punto la sessione è già chiusa.
        """
        if not self.map:
            return self.map_image
        highlight: set[int] | None = None
        if rooms and not session.zones and not (session.full and len(rooms) == len(self.rooms)):
            highlight = set()
            for name in rooms:
                try:
                    highlight.add(self.resolve_room(name))
                except ValueError:
                    continue
        if not highlight and not session.zones:
            return self.map_image
        try:
            image, _ = await self.hass.async_add_executor_job(render, self.map, self.rooms, highlight, session.zones)
        except Exception:  # noqa: BLE001 - in caso di errore va bene la mappa normale
            _LOGGER.exception("Disegno della mappa di fine pulizia non riuscito")
            return self.map_image
        return image

    async def _save_image(self, image: bytes | None = None) -> str | None:
        image = image or self.map_image
        if not image:
            return None
        media = self.hass.config.media_dirs.get("local") or self.hass.config.path("media")
        path = Path(media) / MEDIA_SUBDIR / LAST_CLEAN_IMAGE

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(image)

        try:
            await self.hass.async_add_executor_job(write)
        except OSError as err:
            _LOGGER.warning("Immagine della pulizia non salvata in %s: %s", path, err)
            return None
        return str(path)
