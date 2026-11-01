"""Impostazioni comuni dei test."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def no_real_network():
    """Nei test non si cerca il robot in rete e non si aspetta la partenza reale."""
    with (
        patch("custom_components.alfred_vacuum.device.discover", AsyncMock(return_value=None)) as disc,
        patch("custom_components.alfred_vacuum.coordinator.START_TIMEOUT", 0),
    ):
        yield disc
