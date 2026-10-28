"""Base comune delle entità."""

from __future__ import annotations

from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DID, DOMAIN, MANUFACTURER, MODEL, MODEL_NAME
from .coordinator import AlfredCoordinator
from .device import RobotError


class AlfredEntity(CoordinatorEntity[AlfredCoordinator]):
    """Entità collegata al robot."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AlfredCoordinator, key: str) -> None:
        super().__init__(coordinator)
        did = str(coordinator.entry.data[CONF_DID])
        self._attr_unique_id = f"{did}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, did)},
            manufacturer=MANUFACTURER,
            model=MODEL_NAME,
            model_id=MODEL,
            name=coordinator.entry.title,
        )

    def prop_value(self, prop: tuple[int, int]) -> Any:
        return (self.coordinator.data or {}).get(prop)

    async def run(self, coro) -> None:
        """Esegue un comando e aggiorna lo stato; gli errori diventano messaggi leggibili."""
        try:
            await coro
        except RobotError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
