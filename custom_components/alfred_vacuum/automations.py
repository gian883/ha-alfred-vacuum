"""Automazioni integrate, ognuna attivabile con un interruttore.

- Pulizia all'uscita: quando la presenza diventa "fuori casa" e resta tale per
  qualche minuto, il robot pulisce le stanze scelte (per nome). Al rientro
  torna alla base.
- Stanze trascurate: a un orario fisso pulisce le stanze non pulite da più
  di N giorni, al massimo M per volta, partendo dalla più trascurata.

Entrambe usano i NOMI delle stanze: una nuova mappatura non le rompe.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    A_DOCK,
    CONF_AWAY_DELAY,
    CONF_AWAY_ENTITY,
    CONF_AWAY_MIN_HOURS,
    CONF_AWAY_RETURN,
    CONF_AWAY_ROOMS,
    CONF_SMART_DAYS,
    CONF_SMART_EXCLUDE,
    CONF_SMART_MAX,
    CONF_SMART_TIME,
    DEFAULT_AWAY_DELAY,
    DEFAULT_AWAY_ENTITY,
    DEFAULT_AWAY_MIN_HOURS,
    DEFAULT_SMART_DAYS,
    DEFAULT_SMART_MAX,
    DEFAULT_SMART_TIME,
    MIN_BATTERY_AUTO,
    P_BATTERY,
    P_STATUS,
)
from .coordinator import CLEANING_STATUSES, HOME_STATUSES, AlfredCoordinator, parse_names
from .device import RobotError

_LOGGER = logging.getLogger(__name__)

AWAY_STATES = {"off", "not_home", "away"}
HOME_STATES = {"on", "home"}
FLAG_AWAY = "away_cleaning"
FLAG_SMART = "smart_cleaning"
SOURCE_AWAY = "uscita"
SOURCE_SMART = "stanze trascurate"


def _ready(coordinator: AlfredCoordinator) -> str | None:
    """Motivo per cui il robot NON può partire (None = può partire)."""
    data = coordinator.data or {}
    if data.get(P_STATUS) not in HOME_STATUSES:
        return "il robot non è alla base"
    if int(data.get(P_BATTERY) or 0) < MIN_BATTERY_AUTO:
        return f"batteria sotto il {MIN_BATTERY_AUTO}%"
    return None


class AwayCleaning:
    """Idea 1: pulizia quando esci di casa."""

    def __init__(self, hass: HomeAssistant, coordinator: AlfredCoordinator) -> None:
        self.hass = hass
        self.c = coordinator
        self.opts = coordinator.entry.options
        self.entity_id: str = self.opts.get(CONF_AWAY_ENTITY) or DEFAULT_AWAY_ENTITY
        self._timer: CALLBACK_TYPE | None = None
        self.last_result: str | None = None

    @property
    def enabled(self) -> bool:
        return self.c.flags.get(FLAG_AWAY, False)

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        unsub = async_track_state_change_event(self.hass, [self.entity_id], self._changed)

        def stop() -> None:
            unsub()
            self._cancel()

        return stop

    def _cancel(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None

    @callback
    def _changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None:
            return
        if new.state in AWAY_STATES:
            delay = float(self.opts.get(CONF_AWAY_DELAY, DEFAULT_AWAY_DELAY))
            self._cancel()
            self._timer = async_call_later(self.hass, timedelta(minutes=delay), self._went_out)
        elif new.state in HOME_STATES:
            self._cancel()
            self.hass.async_create_task(self._came_back())

    async def _went_out(self, _now: datetime) -> None:
        self._timer = None
        state = self.hass.states.get(self.entity_id)
        if not self.enabled or not state or state.state not in AWAY_STATES:
            return
        min_hours = float(self.opts.get(CONF_AWAY_MIN_HOURS, DEFAULT_AWAY_MIN_HOURS))
        last = self.c.history.last_away
        if last and dt_util.utcnow() - last < timedelta(hours=min_hours):
            self.last_result = f"saltata: già pulito {min_hours:g} ore fa o meno"
            return
        reason = _ready(self.c)
        if reason:
            self.last_result = f"saltata: {reason}"
            _LOGGER.info("Pulizia all'uscita non avviata: %s", reason)
            return
        rooms = [n for n in parse_names(self.opts.get(CONF_AWAY_ROOMS)) if self._known(n)]
        try:
            if rooms:
                await self.c.async_clean_rooms(rooms, source=SOURCE_AWAY)
            else:
                await self.c.async_clean_all(source=SOURCE_AWAY)
        except (RobotError, ValueError) as err:
            self.last_result = f"errore: {err}"
            _LOGGER.warning("Pulizia all'uscita non riuscita: %s", err)
            return
        self.c.history.last_away = dt_util.utcnow()
        self.c.history.save()
        self.last_result = "avviata: " + (", ".join(rooms) if rooms else "tutta la casa")

    def _known(self, name: str) -> bool:
        try:
            self.c.resolve_room(name)
        except ValueError:
            _LOGGER.warning("Pulizia all'uscita: stanza '%s' non presente nella mappa", name)
            return False
        return True

    async def _came_back(self) -> None:
        session = self.c.session
        if not (session and session.source == SOURCE_AWAY and self.opts.get(CONF_AWAY_RETURN, True)):
            return
        if (self.c.data or {}).get(P_STATUS) in CLEANING_STATUSES | {3, 4}:
            try:
                await self.c.robot.action(A_DOCK)
                self.last_result = "rientro: robot mandato alla base"
            except RobotError as err:
                _LOGGER.warning("Ritorno alla base non riuscito: %s", err)


class SmartCleaning:
    """Idea 3: ogni giorno pulisce le stanze trascurate."""

    def __init__(self, hass: HomeAssistant, coordinator: AlfredCoordinator) -> None:
        self.hass = hass
        self.c = coordinator
        self.opts = coordinator.entry.options
        self.last_result: str | None = None

    @property
    def enabled(self) -> bool:
        return self.c.flags.get(FLAG_SMART, False)

    @property
    def days(self) -> float:
        return float(self.opts.get(CONF_SMART_DAYS, DEFAULT_SMART_DAYS))

    def candidates(self) -> list[tuple[str, datetime | None]]:
        """Stanze trascurate ora, esclusioni tolte, dalla più trascurata."""
        excluded = {n.casefold() for n in parse_names(self.opts.get(CONF_SMART_EXCLUDE))}
        names = [n for n in self.c.room_names if n.casefold() not in excluded]
        return self.c.history.overdue(names, self.days)

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        at = _parse_time(self.opts.get(CONF_SMART_TIME, DEFAULT_SMART_TIME))
        return async_track_time_change(self.hass, self._tick, hour=at.hour, minute=at.minute, second=0)

    async def _tick(self, _now: datetime) -> None:
        await self.async_run()

    async def async_run(self, force: bool = False) -> str:
        if not self.enabled and not force:
            return "spenta"
        reason = _ready(self.c)
        if reason:
            self.last_result = f"saltata: {reason}"
            return self.last_result
        todo = self.candidates()[: int(self.opts.get(CONF_SMART_MAX, DEFAULT_SMART_MAX))]
        if not todo:
            self.last_result = "niente da fare: tutte le stanze sono pulite di recente"
            return self.last_result
        names = [name for name, _ in todo]
        try:
            await self.c.async_clean_rooms(names, source=SOURCE_SMART)
        except (RobotError, ValueError) as err:
            self.last_result = f"errore: {err}"
            return self.last_result
        self.last_result = "avviata: " + ", ".join(names)
        return self.last_result


def _parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    try:
        parts = [int(p) for p in str(value).split(":")]
        return time(parts[0], parts[1] if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return time(11, 0)
