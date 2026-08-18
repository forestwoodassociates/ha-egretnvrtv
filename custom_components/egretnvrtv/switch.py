"""The "Locked Configuration" switch for a TV paired through the Egret NVR TV integration.

Turning this on tells the TV to stop accepting on-device edits to the settings this
integration itself already configured during pairing (Frigate server, Home Assistant
connection, camera filter, local notification listener, and history) — the TV shows a lock
icon on each of those cards and refuses to open/save them while this is on. Meant for a TV
someone else might wander up to and start fiddling with, once its setup is exactly the way
it should be.

Delivery to the TV is two-pronged, matching PAIR_START_PATH/PAIR_COMPLETE_PATH's own local-
push convention (NotificationHttpServer.java) rather than anything the TV has to poll for:
  - Live: every toggle POSTs the new state straight to the TV's own HTTP listener
    (LOCK_CONFIG_PATH) for an immediate effect, best-effort — the TV might be off or
    unreachable right now.
  - Reconciliation: the diagnostics webhook (see __init__.py's _handle_webhook) also answers
    a plain GET with the current state, which the TV calls once it (re)connects — covering a
    toggle that happened while the TV missed the live push above.
"""
from __future__ import annotations

import logging

import aiohttp

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    ATTR_LOCKED,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    DOMAIN,
    LOCK_CONFIG_PATH,
    REQUEST_TIMEOUT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the lock switch for a paired TV."""
    async_add_entities([EgretNvrTvLockSwitch(hass, entry)])


class EgretNvrTvLockSwitch(SwitchEntity, RestoreEntity):
    """Whether this TV should refuse on-device edits to its integration-managed settings."""

    _attr_has_entity_name = True
    _attr_name = "Locked Configuration"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_locked_configuration"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data.get(CONF_DEVICE_NAME) or entry.title,
            manufacturer="Egret NVR TV",
        )

    @property
    def icon(self) -> str:
        return "mdi:lock" if self.is_on else "mdi:lock-open-variant"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        self._attr_is_on = last_state is not None and last_state.state == "on"
        self._write_shared_state()

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self._write_shared_state()
        self.async_write_ha_state()
        await self._async_push_to_tv()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self._write_shared_state()
        self.async_write_ha_state()
        await self._async_push_to_tv()

    # Kept in hass.data alongside the diagnostic sensor values this same entry already tracks
    # (see __init__.py) so the webhook's GET handler can answer "what's the current locked
    # state" without needing its own separate storage or a reference back to this entity.
    def _write_shared_state(self) -> None:
        self._hass.data.setdefault(DOMAIN, {}).setdefault(self._entry.entry_id, {})[
            ATTR_LOCKED
        ] = self._attr_is_on

    async def _async_push_to_tv(self) -> None:
        host = self._entry.data.get(CONF_HOST)
        port = self._entry.data.get(CONF_PORT)
        if not host or not port:
            return
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                f"http://{host}:{port}{LOCK_CONFIG_PATH}",
                json={ATTR_LOCKED: self._attr_is_on},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ):
                pass
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug(
                "Could not push locked-configuration state to %s:%s: %s", host, port, err
            )
