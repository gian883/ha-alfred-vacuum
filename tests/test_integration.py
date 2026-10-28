"""Test dentro Home Assistant con robot simulato."""

from __future__ import annotations

import base64
import json
import zlib
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError

from custom_components.alfred_vacuum.const import (
    CONF_CLOUD,
    CONF_COUNTRY,
    CONF_DID,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    DOMAIN,
)
from custom_components.alfred_vacuum.cloud import CloudSession, TwoFactorChallenge
from custom_components.alfred_vacuum.map_decoder import HEADER

STATE = {
    (2, 1): 6, (2, 2): 0, (2, 3): 1, (3, 1): 87, (3, 2): 1, (4, 5): 2, (4, 6): 1,
    (4, 2): 12, (4, 3): 20, (15, 1): 1, (15, 2): 3, (7, 1): 60, (5, 1): False,
}


def _map() -> str:
    pixels = bytes([1, 1, 4, 4, 2, 2, 6, 6])
    head = HEADER.pack(1, 1, ord("I"), 25, 25, 0, 0, 0, 0, 50, 4, 2, 0, 0)
    seg = {"1": {"type": 1}, "2": {"type": 6}, "4": {"type": 4}, "6": {"type": 8}}
    raw = head + pixels + json.dumps({"seg_inf": seg}).encode()
    return base64.b64encode(zlib.compress(raw)).decode()


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


@pytest.fixture
def fake_robot():
    """Sostituisce il protocollo locale."""
    actions: list = []
    sets: list = []

    async def get_properties(self, did, props):
        return {p: STATE[p] for p in props if p in STATE}

    async def action(self, did, act, ins=None):
        actions.append((act, ins))
        if act == (6, 1):
            return {"code": 0, "out": [{"piid": 1, "value": _map()}]}
        return {"code": 0}

    async def set_property(self, did, prop, value):
        sets.append((prop, value))

    with (
        patch("custom_components.alfred_vacuum.miio.MiioClient.get_properties", get_properties),
        patch("custom_components.alfred_vacuum.miio.MiioClient.action", action),
        patch("custom_components.alfred_vacuum.miio.MiioClient.set_property", set_property),
    ):
        yield actions, sets


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="ALFRED",
        unique_id="123",
        data={
            CONF_DID: "123", CONF_HOST: "192.168.5.80", CONF_TOKEN: "00" * 16,
            CONF_CLOUD: True, CONF_COUNTRY: "de", CONF_USER_ID: "1", CONF_SERVICE_TOKEN: "t", CONF_SSECURITY: "cw==",
        },
    )


async def test_setup_entities_and_rooms(hass: HomeAssistant, fake_robot) -> None:
    actions, sets = fake_robot
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    vac = hass.states.get("vacuum.alfred")
    assert vac is not None and vac.state == "docked"
    assert vac.attributes["fan_speed"] == "Standard"
    assert hass.states.get("sensor.alfred_battery").state == "87"

    # la mappa viene letta in background dopo il primo aggiornamento
    await hass.services.async_call("button", "press", {"entity_id": "button.alfred_refresh_map"}, blocking=True)
    rooms = hass.states.get("sensor.alfred_rooms")
    assert rooms.state == "4"
    assert rooms.attributes["stanze"] == {1: "Soggiorno", 2: "Bagno", 4: "Cucina", 6: "Corridoio"}
    assert hass.states.get("sensor.alfred_current_room").state == "Soggiorno"

    # pulizia per nome
    await hass.services.async_call(DOMAIN, "clean_rooms", {"rooms": ["cucina", "Bagno"]}, blocking=True)
    act, ins = actions[-1]
    assert act == (4, 7)
    assert json.loads(ins[0]["value"]) == {"selects": [[4, 1, 1, 2, 1], [2, 1, 1, 2, 2]]}
    assert ins[1] == {"piid": 1, "value": 18}

    # stanza inesistente -> errore chiaro
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "clean_rooms", {"rooms": ["Garage"]}, blocking=True)

    # selettore + pulsante
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.alfred_room_to_clean", "option": "Corridoio"}, blocking=True
    )
    await hass.services.async_call("button", "press", {"entity_id": "button.alfred_clean_selected_room"}, blocking=True)
    assert json.loads(actions[-1][1][0]["value"])["selects"][0][0] == 6

    # potenza
    await hass.services.async_call(
        "vacuum", "set_fan_speed", {"entity_id": "vacuum.alfred", "fan_speed": "Turbo"}, blocking=True
    )
    assert sets[-1] == ((2, 3), 3)

    # comando di diagnosi con risposta
    resp = await hass.services.async_call(
        DOMAIN, "send_command", {"command": "get_properties", "siid": 3, "id": 1}, blocking=True, return_response=True
    )
    assert resp["result"] == {"3-1": 87}

    # opzioni: nome personalizzato
    hass.config_entries.async_update_entry(entry, options={"room_names": "4=Cucinotto", "scan_interval": 15, "map_interval": 60})
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": "button.alfred_refresh_map"}, blocking=True)
    assert hass.states.get("sensor.alfred_rooms").attributes["stanze"][4] == "Cucinotto"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_cloud_with_2fa(hass: HomeAssistant) -> None:
    session = CloudSession("1", "tok", "cw==", "de")
    devices = [
        {"did": "999", "model": "dreame.vacuum.r2205", "name": "ALFRED", "localip": "192.168.5.80", "token": "ab" * 16},
        {"did": "555", "model": "yeelink.light", "name": "Lampada"},
    ]
    with (
        patch("custom_components.alfred_vacuum.cloud.XiaomiCloud.login", AsyncMock(return_value=TwoFactorChallenge("u", 8, "g***@gmail.com"))),
        patch("custom_components.alfred_vacuum.cloud.XiaomiCloud.verify_2fa", AsyncMock(return_value=session)),
        patch("custom_components.alfred_vacuum.cloud.XiaomiCloud.get_devices", AsyncMock(return_value=devices)),
        patch("custom_components.alfred_vacuum.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "me@example.com", "password": "secret", "country": "de"}
        )
        assert result["step_id"] == "verify"
        assert result["description_placeholders"]["target"] == "g***@gmail.com"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_HOST] == "192.168.5.80"
        assert "password" not in result["data"]  # la password non viene salvata
        assert result["title"] == "ALFRED"


async def test_config_flow_captcha(hass: HomeAssistant, hass_client_no_auth) -> None:
    from custom_components.alfred_vacuum.cloud import CloudCaptchaRequired

    session = CloudSession("1", "tok", "cw==", "de")
    devices = [{"did": "999", "model": "dreame.vacuum.r2205", "name": "ALFRED", "localip": "1.2.3.4", "token": "ab" * 16}]
    calls: list = []

    async def fake_login(self, username, password, captcha=None):
        calls.append(captcha)
        if captcha is None:
            raise CloudCaptchaRequired(b"JPEGDATA")
        if captcha == "sbagliato":
            raise CloudCaptchaRequired(b"JPEG2", wrong=True)
        return session

    assert await __import__("homeassistant.setup", fromlist=["x"]).async_setup_component(hass, "http", {})
    with (
        patch("custom_components.alfred_vacuum.cloud.XiaomiCloud.login", fake_login),
        patch("custom_components.alfred_vacuum.cloud.XiaomiCloud.get_devices", AsyncMock(return_value=devices)),
        patch("custom_components.alfred_vacuum.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "me", "password": "pw", "country": "de"}
        )
        assert result["step_id"] == "captcha"
        url = result["description_placeholders"]["image"].split("(")[1].rstrip(")")
        client = await hass_client_no_auth()
        resp = await client.get(url)
        assert resp.status == 200 and await resp.read() == b"JPEGDATA"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"captcha": "sbagliato"})
        assert result["step_id"] == "captcha" and result["errors"] == {"base": "captcha_wrong"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"captcha": "ab12c"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert calls == [None, "sbagliato", "ab12c"]
        assert (await client.get(url)).status == 404  # immagine eliminata dopo il login
