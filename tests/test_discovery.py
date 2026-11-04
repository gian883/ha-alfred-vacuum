"""Nuovo IP del robot ritrovato in rete, rifiuti del cloud e verifica della partenza."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import patch

import pytest

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.alfred_vacuum import miio
from custom_components.alfred_vacuum.cloud import CloudDeviceError, CloudSession, XiaomiCloud
from custom_components.alfred_vacuum.device import AlfredRobot, RobotError
from custom_components.alfred_vacuum.miio import MiioClient, MiioTimeout

from .test_integration import STATE, _entry, fake_robot  # noqa: F401 - fixture riusata

DID = 123456789


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


class _FakeRobotUdp(asyncio.DatagramProtocol):
    """Risponde al saluto miio come farebbe il robot."""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data == miio.HELLO:
            reply = struct.pack(">HHIII", 0x2131, 32, 0, DID, 1234) + b"\xff" * 16
            self.transport.sendto(reply, addr)


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_discover_finds_robot_by_id() -> None:
    """La ricerca riconosce il robot dal suo id e restituisce l'IP che ha risposto."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(_FakeRobotUdp, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    try:
        with patch.object(miio, "PORT", port):
            assert await miio.discover(str(DID), near="127.0.0.80", timeout=1) == "127.0.0.1"
            # un altro dispositivo miio (id diverso) non viene scambiato per il robot
            assert await miio.discover("999", near="127.0.0.80", timeout=0.4) is None
    finally:
        transport.close()


async def test_timeout_triggers_rediscovery_and_retry(no_real_network) -> None:
    """Robot muto al vecchio IP: lo ritrova al nuovo, salva l'IP e ripete il comando lì."""
    no_real_network.return_value = "192.168.0.81"
    local = MiioClient("192.168.0.80", "00" * 16)
    robot = AlfredRobot(str(DID), local, None)
    saved: list[str] = []
    robot.on_host_changed = saved.append

    async def get_properties(self, did, props):
        if self.host == "192.168.0.80":
            raise MiioTimeout("Nessuna risposta da 192.168.0.80")
        return {(3, 1): 90}

    with patch.object(MiioClient, "get_properties", get_properties):
        assert await robot.get_properties([(3, 1)]) == {(3, 1): 90}
    assert robot.host == "192.168.0.81" and saved == ["192.168.0.81"]
    assert robot.last_route == "local"
    no_real_network.assert_awaited_once()

    # una ricerca senza esito non si ripete a ogni lettura (attesa tra i tentativi)
    no_real_network.reset_mock()
    no_real_network.return_value = None

    async def mute(self, did, props):
        raise MiioTimeout("muto")

    with patch.object(MiioClient, "get_properties", mute):
        for _ in range(3):
            with pytest.raises(RobotError):
                await robot.get_properties([(3, 1)])
    assert no_real_network.await_count == 0  # la ricerca appena riuscita conta per l'attesa


async def test_cloud_inner_code_is_checked() -> None:
    """Il cloud risponde 'ok' ma il robot rifiuta: ora è un errore, non un falso successo."""
    cloud = XiaomiCloud("de", CloudSession(user_id="1", service_token="t", ssecurity="cw==", country="de"))

    async def api(path, params):
        return {"did": str(DID), "siid": 4, "aiid": 1, "code": -704042011}

    with patch.object(cloud, "api", api):
        with pytest.raises(CloudDeviceError, match="-704042011"):
            await cloud.action(str(DID), (4, 1))

    robot = AlfredRobot(str(DID), None, cloud)
    with patch.object(cloud, "api", api):
        with pytest.raises(RobotError, match="rifiutato"):
            await robot.action((4, 1))


async def test_cloud_properties_all_failed_is_error() -> None:
    cloud = XiaomiCloud("de", CloudSession(user_id="1", service_token="t", ssecurity="cw==", country="de"))

    async def api(path, params):
        return [{"siid": 2, "piid": 1, "code": -704042011}]

    with patch.object(cloud, "api", api), pytest.raises(CloudDeviceError):
        await cloud.get_properties(str(DID), [(2, 1)])


async def test_clean_rooms_fails_if_robot_does_not_leave(hass: HomeAssistant, fake_robot) -> None:  # noqa: F811
    """Comando accettato ma robot fermo alla base: errore chiaro, non silenzio."""
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": "button.alfred_refresh_map"}, blocking=True)

    with (
        patch("custom_components.alfred_vacuum.coordinator.START_TIMEOUT", 0.03),
        patch("custom_components.alfred_vacuum.coordinator.START_POLL", 0.01),
    ):
        with pytest.raises(HomeAssistantError, match="ancora alla base"):
            await hass.services.async_call(DOMAIN_, "clean_rooms", {"rooms": ["Cucina"]}, blocking=True)

        # se invece parte (stato 1 = aspirazione) il servizio riesce
        with patch.dict(STATE, {(2, 1): 1}):
            await hass.services.async_call(DOMAIN_, "clean_rooms", {"rooms": ["Cucina"]}, blocking=True)


async def test_new_ip_saved_without_reload(hass: HomeAssistant, fake_robot, no_real_network) -> None:  # noqa: F811
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert hass.states.get("sensor.alfred_connection").state == "locale"

    no_real_network.return_value = "192.168.0.81"
    real = MiioClient.get_properties

    async def moved(self, did, props):
        if self.host == "192.168.0.80":
            raise MiioTimeout("Nessuna risposta da 192.168.0.80")
        return await real(self, did, props)

    with (
        patch.object(MiioClient, "get_properties", moved),
        patch.object(hass.config_entries, "async_reload") as reload,
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert entry.data[CONF_HOST] == "192.168.0.81"
    reload.assert_not_called()
    assert entry.runtime_data.coordinator is coordinator
    state = hass.states.get("sensor.alfred_connection")
    assert state.state == "locale" and state.attributes["indirizzo_ip"] == "192.168.0.81"


DOMAIN_ = "alfred_vacuum"
