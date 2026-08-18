"""The Egret NVR TV integration.

This integration pairs a TV running the Egret NVR TV Android app with this Home Assistant
instance without the user having to generate or copy a Long-Lived Access Token by hand, or
type this instance's URL into the TV — see config_flow.py for the actual discovery/
PIN-verified pairing exchange.

Once paired, the TV talks to Home Assistant's existing REST/WebSocket APIs directly using
the token it was given, exactly as if the user had entered it manually. The one thing this
integration does at runtime is receive that TV's own diagnostic status (last notification,
last clip played, connection health, ...) over a dedicated webhook and expose it as sensor/
binary_sensor entities on that TV's device — see sensor.py/binary_sensor.py.
"""
from __future__ import annotations

import logging
from functools import partial

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ATTR_LOCKED,
    CONF_WEBHOOK_ID,
    DOMAIN,
    KNOWN_DIAGNOSTIC_FIELDS,
    signal_update,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Egret NVR TV from a config entry."""
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {}

    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if webhook_id:
        webhook.async_register(
            hass,
            DOMAIN,
            entry.title,
            webhook_id,
            partial(_handle_webhook, entry_id=entry.entry_id),
            # The TV only ever learns this webhook_id over the local pairing exchange, and
            # only ever calls it while on the same network as this Home Assistant instance
            # (same constraint the pairing flow itself already has) — no reason to accept it
            # through a remote/cloud path. GET is for the TV reconciling the lock switch's
            # current state (see switch.py); POST is the existing diagnostics ingestion below.
            local_only=True,
            allowed_methods=["GET", "POST"],
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if webhook_id:
        webhook.async_unregister(hass, webhook_id)

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def _handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request, entry_id: str
) -> web.Response:
    """Receive a diagnostic-status push from a paired TV (POST), or answer the TV's own
    reconciling lookup of the current locked-configuration state (GET) — see switch.py's own
    doc comment for why that state needs a pull path in addition to its live push.
    """
    if request.method == "GET":
        values = hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})
        return web.json_response({ATTR_LOCKED: values.get(ATTR_LOCKED, False)})

    # Every field is optional and merged into this entry's currently-known values — the TV
    # sends small, targeted updates (a new notification, a connection status change, ...)
    # rather than resending its full state every time. Unknown keys are ignored rather than
    # rejected, so an older-paired TV posting a field this version doesn't know about yet
    # doesn't fail the whole request.
    try:
        payload = await request.json()
    except ValueError:
        return web.Response(status=400)
    if not isinstance(payload, dict):
        return web.Response(status=400)

    values = hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})
    changed = False
    for key in KNOWN_DIAGNOSTIC_FIELDS:
        if key in payload:
            values[key] = payload[key]
            changed = True

    if changed:
        async_dispatcher_send(hass, signal_update(entry_id))

    return web.Response(status=200)
