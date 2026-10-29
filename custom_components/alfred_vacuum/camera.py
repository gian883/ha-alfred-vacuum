"""Mappa del robot come immagine (per dashboard, card interattiva e Telegram)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlfredConfigEntry
from .entity import AlfredEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: AlfredConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([AlfredMap(entry.runtime_data.coordinator)])


class AlfredMap(AlfredEntity, Camera):
    """Immagine PNG della mappa con stanze, nomi, base e robot."""

    _attr_icon = "mdi:map"
    _attr_frame_interval = 5
    content_type = "image/png"

    def __init__(self, coordinator) -> None:
        AlfredEntity.__init__(self, coordinator, "map")
        Camera.__init__(self)
        self._image_ref: bytes | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        # nuova immagine -> nuovo token di accesso per forzare l'aggiornamento nel browser
        if self.coordinator.map_image is not self._image_ref:
            self._image_ref = self.coordinator.map_image
            self.async_update_token()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        return self.coordinator.map_image is not None

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self.coordinator.map_image

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self.coordinator
        return {
            **coord.map_attributes,
            "mappa_aggiornata": coord.map_updated.isoformat() if coord.map_updated else None,
        }
