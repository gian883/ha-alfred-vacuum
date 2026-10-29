"""Robot o cloud che non rispondono per un po': le entità non devono sparire subito."""

from __future__ import annotations

from unittest.mock import patch

import aiohttp
import pytest

from homeassistant.core import HomeAssistant

from custom_components.alfred_vacuum.cloud import CloudError, CloudSession, XiaomiCloud
from custom_components.alfred_vacuum.coordinator import MAX_MISSED_UPDATES
from custom_components.alfred_vacuum.miio import MiioTimeout

from .test_integration import _entry, fake_robot  # noqa: F401 - fixture riusata


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


async def test_cloud_network_error_becomes_cloud_error() -> None:
    """DNS/SSL/timeout verso il cloud diventano CloudError (prima uscivano grezzi)."""
    cloud = XiaomiCloud("de", CloudSession(user_id="1", service_token="t", ssecurity="cw==", country="de"))

    class Boom:
        def post(self, *args, **kwargs):
            raise aiohttp.ClientConnectionError("Timeout while contacting DNS servers")

    with patch.object(XiaomiCloud, "_client", lambda self: Boom()):
        with pytest.raises(CloudError, match="non raggiungibile"):
            await cloud.api("miotspec/prop/get", {"params": []})


async def test_short_outage_keeps_last_values(hass: HomeAssistant, fake_robot) -> None:  # noqa: F811
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert hass.states.get("vacuum.alfred").state == "docked"

    async def offline(self, did, props):
        raise MiioTimeout("Nessuna risposta")

    with (
        patch("custom_components.alfred_vacuum.miio.MiioClient.get_properties", offline),
        patch.object(XiaomiCloud, "get_properties", side_effect=CloudError("Cloud Xiaomi non raggiungibile")),
    ):
        # qualche aggiornamento fallito: valori tenuti, entità disponibili
        for _ in range(MAX_MISSED_UPDATES - 1):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert coordinator.last_update_success
        assert hass.states.get("vacuum.alfred").state == "docked"
        assert hass.states.get("sensor.alfred_battery").state == "87"

        # guasto che dura: entità non disponibili
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert not coordinator.last_update_success
        assert hass.states.get("vacuum.alfred").state == "unavailable"

    # il robot torna: tutto di nuovo disponibile e contatore azzerato
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert hass.states.get("vacuum.alfred").state == "docked"
    assert coordinator._missed_updates == 0
