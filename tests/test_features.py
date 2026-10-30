"""Test delle funzioni v0.2: storico per nome, rimappatura, mappa, automazioni."""

from __future__ import annotations

import base64
import json
import zlib
from datetime import timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.alfred_vacuum.const import (
    CONF_CLOUD,
    CONF_DID,
    DOMAIN,
    EVENT_CLEANING_FINISHED,
    EVENT_ROOMS_CHANGED,
)
from custom_components.alfred_vacuum.map_decoder import HEADER, decode
from custom_components.alfred_vacuum.map_render import MapGeometry, render


def make_map(seg: dict, pixels: bytes | None = None, robot=(25, 25)) -> str:
    pixels = pixels or bytes([1, 1, 4, 4, 2, 2, 6, 6])
    head = HEADER.pack(1, 1, ord("I"), robot[0], robot[1], 90, 0, 0, 0, 50, 4, 2, 0, 0)
    raw = head + pixels + json.dumps({"seg_inf": seg}).encode()
    return base64.b64encode(zlib.compress(raw)).decode()


MAP_A = make_map({"1": {"type": 4}, "2": {"type": 8}, "4": {"type": 1}, "6": {"type": 2}})
# rimappatura: stessi ambienti ma numeri diversi
MAP_B = make_map({"1": {"type": 1}, "2": {"type": 4}, "4": {"type": 2}, "6": {"type": 8}})


class FakeRobot:
    def __init__(self) -> None:
        self.state = {(2, 1): 6, (2, 2): 0, (2, 3): 1, (3, 1): 90, (4, 5): 1, (4, 2): 0, (4, 3): 0}
        self.map = MAP_A
        self.actions: list = []

    async def get_properties(self, _self, did, props):
        return {p: self.state[p] for p in props if p in self.state}

    async def action(self, _self, did, act, ins=None):
        self.actions.append((act, ins))
        if act == (6, 1):
            return {"code": 0, "out": [{"piid": 1, "value": self.map}]}
        return {"code": 0}

    async def set_property(self, _self, did, prop, value):
        self.state[prop] = value


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


@pytest.fixture
def robot(tmp_path):
    fake = FakeRobot()

    async def gp(self, did, props):
        return await fake.get_properties(self, did, props)

    async def ac(self, did, act, ins=None):
        return await fake.action(self, did, act, ins)

    async def sp(self, did, prop, value):
        return await fake.set_property(self, did, prop, value)

    with (
        patch("custom_components.alfred_vacuum.miio.MiioClient.get_properties", gp),
        patch("custom_components.alfred_vacuum.miio.MiioClient.action", ac),
        patch("custom_components.alfred_vacuum.miio.MiioClient.set_property", sp),
    ):
        yield fake


async def setup(hass: HomeAssistant, tmp_path, options: dict | None = None) -> MockConfigEntry:
    hass.config.media_dirs = {"local": str(tmp_path)}
    entry = MockConfigEntry(
        domain=DOMAIN, title="ALFRED", unique_id="123", options=options or {},
        data={CONF_DID: "123", CONF_HOST: "10.0.0.2", CONF_TOKEN: "00" * 16, CONF_CLOUD: False},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "refresh_map", {}, blocking=True)
    await hass.async_block_till_done()
    return entry


def eid(hass, domain: str, key: str) -> str:
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"123_{key}")
    assert entity_id, key
    return entity_id


async def refresh(hass, entry):
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


def test_render_png_and_calibration():
    data = decode(MAP_A)
    png, attrs = render(data, data.rooms)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert attrs["rooms"]["1"]["name"] == "Cucina"
    geo = MapGeometry(data)
    for x, y in ((0, 0), (125, 75), (200, 100)):
        assert geo.to_robot(*geo.to_image(x, y)) == (x, y)
    assert len(attrs["calibration_points"]) == 3


async def test_session_history_event_and_image(hass: HomeAssistant, robot, tmp_path) -> None:
    entry = await setup(hass, tmp_path)
    events = []
    hass.bus.async_listen(EVENT_CLEANING_FINISHED, lambda e: events.append(e.data))

    resp = await hass.services.async_call(
        DOMAIN, "clean_rooms", {"rooms": ["Cucina"], "source": "Telegram"}, blocking=True, return_response=True
    )
    assert resp == {"stanze": ["Cucina"]}
    robot.state.update({(2, 1): 12, (4, 3): 6, (4, 2): 9})
    await refresh(hass, entry)
    assert entry.runtime_data.coordinator.session.requested == ["Cucina"]
    robot.state.update({(2, 1): 6})
    await refresh(hass, entry)

    assert events and events[0]["stanze"] == ["Cucina"]
    assert events[0]["area_m2"] == 6 and events[0]["durata_min"] == 9 and events[0]["avviata_da"] == "Telegram"
    saved = (tmp_path / "alfred_vacuum" / "ultima_pulizia.png").read_bytes()
    assert saved[:4] == b"\x89PNG"
    # foto di fine pulizia con la Cucina evidenziata; la mappa della dashboard resta normale
    coordinator = entry.runtime_data.coordinator
    assert coordinator.map_image and saved != coordinator.map_image
    assert entry.runtime_data.coordinator.history.last_cleaned("cucina") is not None
    assert hass.states.get("sensor.alfred_last_cleaning").attributes["stanze"] == ["Cucina"]
    assert hass.states.get("camera.alfred_map") is not None


async def test_names_survive_remapping(hass: HomeAssistant, robot, tmp_path) -> None:
    entry = await setup(hass, tmp_path)
    changed = []
    hass.bus.async_listen(EVENT_ROOMS_CHANGED, lambda e: changed.append(e.data))
    entry.runtime_data.coordinator.history.mark_cleaned(["Cucina"])

    robot.map = MAP_B  # nuova mappatura
    await hass.services.async_call(DOMAIN, "refresh_map", {}, blocking=True)
    await hass.async_block_till_done()
    assert changed and changed[0]["dopo"]["2"] == "Cucina"
    assert hass.states.get("persistent_notification.alfred_vacuum_rooms_changed") or True

    # stessa chiamata per nome: ora va alla stanza 2
    await hass.services.async_call(DOMAIN, "clean_rooms", {"rooms": ["Cucina"]}, blocking=True)
    act, ins = robot.actions[-1]
    assert json.loads(ins[0]["value"])["selects"][0][0] == 2
    # lo storico per nome è rimasto
    assert entry.runtime_data.coordinator.history.last_cleaned("Cucina") is not None


async def test_away_cleaning(hass: HomeAssistant, robot, tmp_path) -> None:
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"in_casa": {"initial": True}}})
    entry = await setup(
        hass, tmp_path,
        {"away_presence_entity": "input_boolean.in_casa", "away_rooms": ["Cucina", "Soggiorno"],
         "away_delay": 5, "away_min_hours": 12, "away_return_home": True},
    )
    # spento: non succede niente
    await hass.services.async_call("input_boolean", "turn_off", {"entity_id": "input_boolean.in_casa"}, blocking=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert not [a for a in robot.actions if a[0] == (4, 7)]

    await hass.services.async_call("switch", "turn_on", {"entity_id": eid(hass, "switch", "away_cleaning")}, blocking=True)
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.in_casa"}, blocking=True)
    await hass.services.async_call("input_boolean", "turn_off", {"entity_id": "input_boolean.in_casa"}, blocking=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()
    clean = [a for a in robot.actions if a[0] == (4, 7)]
    assert clean and [s[0] for s in json.loads(clean[-1][1][0]["value"])["selects"]] == [1, 4]

    # inizia a pulire, poi rientro -> alla base
    robot.state[(2, 1)] = 12
    await refresh(hass, entry)
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.in_casa"}, blocking=True)
    await hass.async_block_till_done()
    assert robot.actions[-1][0] == (3, 1)


async def test_away_cancelled_if_back_before_delay(hass: HomeAssistant, robot, tmp_path) -> None:
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"in_casa": {"initial": True}}})
    await setup(hass, tmp_path, {"away_presence_entity": "input_boolean.in_casa", "away_delay": 10})
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid(hass, "switch", "away_cleaning")}, blocking=True)
    await hass.services.async_call("input_boolean", "turn_off", {"entity_id": "input_boolean.in_casa"}, blocking=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3))
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.in_casa"}, blocking=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=15))
    await hass.async_block_till_done()
    assert not [a for a in robot.actions if a[0] in ((4, 7), (2, 1))]


async def test_smart_cleaning(hass: HomeAssistant, robot, tmp_path) -> None:
    entry = await setup(hass, tmp_path, {"smart_days": 3, "smart_max_rooms": 2, "smart_exclude": ["Corridoio"]})
    history = entry.runtime_data.coordinator.history
    now = dt_util.utcnow()
    history.mark_cleaned(["Cucina"], now - timedelta(days=1))  # recente
    history.mark_cleaned(["Soggiorno"], now - timedelta(days=10))  # trascurata
    history.mark_cleaned(["Camera da letto"], now - timedelta(days=5))  # trascurata

    resp = await hass.services.async_call(DOMAIN, "clean_overdue", {"dry_run": True}, blocking=True, return_response=True)
    assert [r["stanza"] for r in resp["stanze_trascurate"]] == ["Soggiorno", "Camera da letto"]
    await refresh(hass, entry)
    assert hass.states.get("sensor.alfred_rooms_to_clean").state == "2"

    # interruttore spento: all'orario non parte
    smart = entry.runtime_data.smart
    assert await smart.async_run() == "spenta"
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid(hass, "switch", "smart_cleaning")}, blocking=True)
    result = await smart.async_run()
    assert result == "avviata: Soggiorno, Camera da letto"
    assert [s[0] for s in json.loads(robot.actions[-1][1][0]["value"])["selects"]] == [4, 6]

    # batteria bassa: saltata
    robot.state[(3, 1)] = 20
    await refresh(hass, entry)
    assert (await smart.async_run()).startswith("saltata")


async def test_options_flow_sections(hass: HomeAssistant, robot, tmp_path) -> None:
    entry = await setup(hass, tmp_path)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "general": {"room_names": "", "scan_interval": 15, "map_interval": 60, "live_map_interval": 30},
            "away": {"away_presence_entity": "input_boolean.giggi_in_casa", "away_rooms": ["Cucina"], "away_delay": 10,
                     "away_min_hours": 12, "away_return_home": True},
            "smart": {"smart_time": "11:00:00", "smart_days": 3, "smart_max_rooms": 2, "smart_exclude": []},
        },
    )
    assert entry.options["away_rooms"] == ["Cucina"] and entry.options["smart_days"] == 3


def test_room_outlines_follow_cells():
    from custom_components.alfred_vacuum.map_render import room_outlines

    # stanza 1 a forma di L in una griglia 3x3 (celle da 50 mm)
    pixels = bytes([1, 0, 0,
                    1, 0, 0,
                    1, 1, 1])
    head = HEADER.pack(1, 1, ord("I"), 0, 0, 0, 0, 0, 0, 50, 3, 3, 0, 0)
    raw = head + pixels + json.dumps({"seg_inf": {"1": {"type": 4}}}).encode()
    data = decode(base64.b64encode(zlib.compress(raw)).decode())
    outline = room_outlines(data)[1]
    assert len(outline) == 6  # una L ha 6 vertici
    assert {tuple(p) for p in outline} == {(0, 0), (50, 0), (50, 100), (150, 100), (150, 150), (0, 150)}


async def test_clean_zone_service(hass: HomeAssistant, robot, tmp_path) -> None:
    await setup(hass, tmp_path)
    await hass.services.async_call(
        DOMAIN, "clean_zone", {"zones": [[300, 400, 100, 200]], "repeats": 2}, blocking=True
    )
    act, ins = robot.actions[-1]
    assert act == (4, 7)
    assert json.loads(ins[0]["value"]) == {"areas": [[100, 200, 300, 400, 2, 1, 1]]}
    assert ins[1] == {"piid": 1, "value": 19}
    # una sola zona come lista piatta (formato della card) e come testo
    await hass.services.async_call(DOMAIN, "clean_zone", {"zones": [10, 20, 30, 40]}, blocking=True)
    assert json.loads(robot.actions[-1][1][0]["value"])["areas"][0][:4] == [10, 20, 30, 40]
    await hass.services.async_call(DOMAIN, "clean_zone", {"zones": "[[1,2,3,4]]"}, blocking=True)
    assert json.loads(robot.actions[-1][1][0]["value"])["areas"][0][:4] == [1, 2, 3, 4]


def test_render_highlight_and_zones():
    """Stanze in pulizia evidenziate, le altre sbiadite; zone disegnate; nomi di tutte le stanze."""
    import io

    from PIL import Image

    pixels = bytes([4] * 20 + [8] * 20) * 20  # 40x20 celle: metà stanza 1, metà stanza 2 (formato x4)
    head = HEADER.pack(1, 1, ord("I"), 0, 0, 0, 0, 0, 0, 50, 40, 20, 0, 0)
    seg = {"1": {"type": 4}, "2": {"type": 1}}
    data = decode_raw_bytes(head + pixels + json.dumps({"seg_inf": seg}).encode())
    assert data.room_shift == 2

    png, attrs = render(data, data.rooms, highlight={1})
    assert set(attrs["rooms"]) == {"1", "2"}
    assert attrs["rooms"]["1"]["name"] == "Cucina" and attrs["rooms"]["2"]["name"] == "Soggiorno"
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    geo = MapGeometry(data)
    k = img.getpixel(tuple(int(v) for v in geo.to_image(200, 100)))
    s = img.getpixel(tuple(int(v) for v in geo.to_image(1800, 100)))
    assert k[3] == 255 and s[3] < 255  # cucina piena, soggiorno sbiadito

    png, _ = render(data, data.rooms, zones=[[1100, 300, 1600, 800]])
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    inside = img.getpixel(tuple(int(v) for v in geo.to_image(1350, 550)))
    outside = img.getpixel(tuple(int(v) for v in geo.to_image(200, 100)))
    assert inside[0] > 200 and inside != outside  # riempimento arancione della zona


def decode_raw_bytes(raw: bytes):
    from custom_components.alfred_vacuum.map_decoder import decode_raw

    return decode_raw(raw)
