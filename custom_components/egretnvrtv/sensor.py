"""Diagnostic sensors for a TV paired through the Egret NVR TV integration.

Values are pushed by the TV itself over this entry's own webhook (see __init__.py) rather
than polled — entities here are pure read-through views onto the shared per-entry values
dict, updated via a dispatcher signal each time a new webhook POST changes something.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_APP_VERSION,
    ATTR_LAST_CLIP_PLAYED_AT,
    ATTR_LAST_CLIP_URL,
    ATTR_LAST_NOTIFICATION_AT,
    ATTR_LAST_NOTIFICATION_TITLE,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    DOMAIN,
    signal_update,
)


@dataclass(frozen=True, kw_only=True)
class EgretNvrTvSensorEntityDescription(SensorEntityDescription):
    """Adds an optional extra-attributes hook to the stock sensor description."""

    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSOR_DESCRIPTIONS: tuple[EgretNvrTvSensorEntityDescription, ...] = (
    EgretNvrTvSensorEntityDescription(
        key=ATTR_LAST_NOTIFICATION_AT,
        name="Last Notification Received",
        icon="mdi:bell-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EgretNvrTvSensorEntityDescription(
        key=ATTR_LAST_NOTIFICATION_TITLE,
        name="Last Notification",
        icon="mdi:bell-ring-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EgretNvrTvSensorEntityDescription(
        key=ATTR_LAST_CLIP_PLAYED_AT,
        name="Last Clip Played",
        icon="mdi:play-circle-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Rides as an attribute rather than its own sensor — Frigate VOD URLs are long,
        # unbroken strings that don't belong in a top-level state value. Only shown in this
        # entity's own detail view, not any compact diagnostics list.
        attributes_fn=lambda values: (
            {"clip_url": values[ATTR_LAST_CLIP_URL]} if values.get(ATTR_LAST_CLIP_URL) else {}
        ),
    ),
    EgretNvrTvSensorEntityDescription(
        key=ATTR_APP_VERSION,
        name="App Version",
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for a paired TV."""
    values = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EgretNvrTvSensor(entry, description, values) for description in SENSOR_DESCRIPTIONS
    )


class EgretNvrTvSensor(SensorEntity):
    """A single diagnostic value pushed by a paired TV."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    entity_description: EgretNvrTvSensorEntityDescription

    def __init__(
        self,
        entry: ConfigEntry,
        description: EgretNvrTvSensorEntityDescription,
        values: dict[str, Any],
    ) -> None:
        self.entity_description = description
        self._entry = entry
        self._values = values
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data.get(CONF_DEVICE_NAME) or entry.title,
            manufacturer="Egret NVR TV",
        )

    @property
    def native_value(self) -> Any:
        raw = self._values.get(self.entity_description.key)
        if raw is None:
            return None
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            return dt_util.parse_datetime(raw)
        return raw

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self._values) or None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self._entry.entry_id), self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
