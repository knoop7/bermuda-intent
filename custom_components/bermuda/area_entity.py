"""Area detection based on HA entity states.

Provides area presence indicators by monitoring configured entity IDs.
When an entity is "on" (triggered), its area is used as a candidate
in Bermuda's area detection logic alongside BLE distance-based detection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import STATE_ON
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .const import _LOGGER, CONF_AREA_ENTITIES

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STATES_TRIGGERED = {STATE_ON, "true"}


class BermudaAreaEntityManager:
    """Manages entity-based area presence indicators."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._er = er.async_get(hass)
        self._dr = dr.async_get(hass)
        self._ar = ar.async_get(hass)

    def resolve_entity_area(self, entity_id: str) -> tuple[str | None, str | None]:
        """Resolve area_id and area_name for a given entity_id.

        Checks entity's own area first, then falls back to its device's area.
        Returns (area_id, area_name) or (None, None).
        """
        entry = self._er.async_get(entity_id)
        if entry is None:
            return None, None

        area_id = entry.area_id
        if area_id is None and entry.device_id is not None:
            device = self._dr.async_get(entry.device_id)
            if device is not None:
                area_id = device.area_id

        if area_id is None:
            return None, None

        area = self._ar.async_get_area(area_id)
        if area is None:
            return None, None

        return area_id, area.name

    def get_triggered_areas(self, configured_entities: list[str]) -> dict[str, str]:
        """Return a mapping of area_id -> area_name for all triggered entities.

        An entity is considered triggered when its state is 'on' or 'true'.
        Only entities with a resolvable area are included.
        """
        triggered: dict[str, str] = {}
        for entity_id in configured_entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                _LOGGER.debug("Area entity %s has no state (not loaded?)", entity_id)
                continue
            if state.state.lower() in STATES_TRIGGERED:
                area_id, area_name = self.resolve_entity_area(entity_id)
                if area_id is not None and area_name is not None:
                    triggered[area_id] = area_name
                else:
                    _LOGGER.debug(
                        "Area entity %s is triggered but has no resolvable area",
                        entity_id,
                    )
        return triggered
