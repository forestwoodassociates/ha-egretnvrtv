"""Diagnostic binary sensors for a TV paired through the Egret NVR TV integration.

Values are pushed by the TV itself over this entry's own webhook (see __init__.py) rather
than polled — see sensor.py's own module doc comment for the shared design.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_FRIGATE_CONNECTED,
    ATTR_HOME_ASSISTANT_CONNECTED,
    ATTR_NOTIFICATION_SERVER_RUNNING,
    ATTR_OVERLAY_PERMISSION_GRANTED,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    DOMAIN,
    signal_update,
)

BINARY_SENSOR_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key=ATTR_HOME_ASSISTANT_CONNECTED,
        name="Home Assistant Connected",
        icon="mdi:lan-connect",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key=ATTR_FRIGATE_CONNECTED,
        name="Frigate Connected",
        icon="mdi:cctv",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key=ATTR_OVERLAY_PERMISSION_GRANTED,
        name="Overlay Permission Granted",
        icon="mdi:check-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key=ATTR_NOTIFICATION_SERVER_RUNNING,
        name="Notification Server Running",
        icon="mdi:server-network",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up binary sensors for a paired TV."""
    values = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EgretNvrTvBinarySensor(entry, description, values)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class EgretNvrTvBinarySensor(BinarySensorEntity):
    """A single diagnostic on/off state pushed by a paired TV."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        description: BinarySensorEntityDescription,
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
    def is_on(self) -> bool:
        # False (not None) until the TV's first diagnostics webhook arrives — a paired TV
        # that hasn't reported in yet is indistinguishable from one that has reported "not
        # connected", and defaulting to False avoids every one of these showing as a
        # grey/unhelpful "Unknown" state on first setup (or after HA restarts and the TV
        # hasn't posted since).
        return bool(self._values.get(self.entity_description.key))

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self._entry.entry_id), self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
