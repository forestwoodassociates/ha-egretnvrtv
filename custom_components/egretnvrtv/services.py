"""The egretnvrtv.show_camera/hide_camera services: show a camera's live view on one or more
paired TVs, or close whatever live view they're showing.

Targets are ordinary Home Assistant targets — a TV's device or entities, or an area, which
is what makes "show the driveway" from a voice assistant land on the TV in the same room: an
Assist automation can target ``area_id(trigger.device_id)``, the area of the voice device that
heard the command. Each targeted TV gets a POST to its own LIVE_VIEW_PATH, authenticated with
the webhook id it was given at pairing; the TV answers once it has found the camera's stream,
so an unknown camera comes back as an error the caller can report.

Targets are resolved here from the device/entity/area registries rather than through
homeassistant.helpers.service's extract helpers, whose signatures have changed across the
Home Assistant versions this integration supports (hacs.json).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ATTR_CAMERA,
    ATTR_DURATION,
    ATTR_MODE,
    CONF_WEBHOOK_ID,
    DEFAULT_LIVE_VIEW_DURATION_SECONDS,
    DOMAIN,
    LIVE_VIEW_HIDE_PATH,
    LIVE_VIEW_PATH,
    LIVE_VIEW_SECRET_HEADER,
    LIVE_VIEW_TIMEOUT_SECONDS,
    MAX_LIVE_VIEW_DURATION_SECONDS,
    MIN_LIVE_VIEW_DURATION_SECONDS,
    MODE_FULLSCREEN,
    MODE_POPUP,
    SERVICE_HIDE_CAMERA,
    SERVICE_SHOW_CAMERA,
)

_LOGGER = logging.getLogger(__name__)

SHOW_CAMERA_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_CAMERA): cv.string,
        vol.Optional(ATTR_MODE, default=MODE_POPUP): vol.In([MODE_POPUP, MODE_FULLSCREEN]),
        vol.Optional(ATTR_DURATION, default=DEFAULT_LIVE_VIEW_DURATION_SECONDS): vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_LIVE_VIEW_DURATION_SECONDS, max=MAX_LIVE_VIEW_DURATION_SECONDS),
        ),
    }
)

HIDE_CAMERA_SCHEMA = cv.make_entity_service_schema({})


def async_register_services(hass: HomeAssistant) -> None:
    """Register this integration's services (once, from async_setup)."""

    async def handle_show_camera(call: ServiceCall) -> ServiceResponse:
        entries = _resolve_target_entries(hass, call)
        if not entries:
            raise ServiceValidationError(
                "No Egret NVR TV found for that target — pick a TV, or an area that has one"
            )
        body = {
            ATTR_CAMERA: call.data[ATTR_CAMERA],
            ATTR_MODE: call.data[ATTR_MODE],
            ATTR_DURATION: call.data[ATTR_DURATION],
        }
        results = await asyncio.gather(
            *(_async_call_tv(hass, entry, LIVE_VIEW_PATH, body) for entry in entries)
        )
        response = {entry.title: result for entry, result in zip(entries, results)}
        if not any(result["success"] for result in results):
            # Surfaced to the caller (and so to a voice assistant's reply) instead of only logged.
            messages = "; ".join(f"{name}: {r['message']}" for name, r in response.items())
            raise HomeAssistantError(f"Couldn't show {body[ATTR_CAMERA]}: {messages}")
        for name, result in response.items():
            if not result["success"]:
                _LOGGER.warning("show_camera on %s failed: %s", name, result["message"])
        return {"tvs": response} if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_SHOW_CAMERA,
        handle_show_camera,
        schema=SHOW_CAMERA_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def handle_hide_camera(call: ServiceCall) -> ServiceResponse:
        entries = _resolve_target_entries(hass, call)
        if not entries:
            raise ServiceValidationError(
                "No Egret NVR TV found for that target — pick a TV, or an area that has one"
            )
        results = await asyncio.gather(
            *(_async_call_tv(hass, entry, LIVE_VIEW_HIDE_PATH, {}) for entry in entries)
        )
        response = {entry.title: result for entry, result in zip(entries, results)}
        # A TV with nothing open is fine; only a TV that couldn't be reached at all is a failure.
        if not any(result["success"] for result in results):
            messages = "; ".join(f"{name}: {r['message']}" for name, r in response.items())
            raise HomeAssistantError(f"Couldn't close the live view: {messages}")
        return {"tvs": response} if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_HIDE_CAMERA,
        handle_hide_camera,
        schema=HIDE_CAMERA_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _resolve_target_entries(hass: HomeAssistant, call: ServiceCall) -> list[ConfigEntry]:
    """The loaded Egret NVR TV config entries (one per TV) the call's target refers to."""
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    device_ids: set[str] = set(_as_list(call.data.get(ATTR_DEVICE_ID)))

    for entity_id in _as_list(call.data.get(ATTR_ENTITY_ID)):
        entity = entity_reg.async_get(entity_id)
        if entity is not None and entity.device_id:
            device_ids.add(entity.device_id)

    for area_id in _as_list(call.data.get(ATTR_AREA_ID)):
        device_ids.update(device.id for device in dr.async_entries_for_area(device_reg, area_id))
        # An entity can be put in a room of its own, apart from its device's.
        device_ids.update(
            entity.device_id
            for entity in er.async_entries_for_area(entity_reg, area_id)
            if entity.device_id
        )

    entries: dict[str, ConfigEntry] = {}
    for device_id in device_ids:
        device = device_reg.async_get(device_id)
        if device is None:
            continue
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == DOMAIN:
                entries[entry.entry_id] = entry
    return list(entries.values())


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


async def _async_call_tv(
    hass: HomeAssistant, entry: ConfigEntry, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    host = entry.data.get(CONF_HOST)
    port = entry.data.get(CONF_PORT)
    secret = entry.data.get(CONF_WEBHOOK_ID)
    if not host or not port or not secret:
        return {"success": False, "message": "This TV's pairing is missing its address or key; re-pair it"}
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            f"http://{host}:{port}{path}",
            json=body,
            headers={LIVE_VIEW_SECRET_HEADER: secret},
            timeout=aiohttp.ClientTimeout(total=LIVE_VIEW_TIMEOUT_SECONDS),
        ) as response:
            try:
                payload = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                payload = {}
            if response.status == 200:
                if path == LIVE_VIEW_HIDE_PATH:
                    return {"success": True, "closed": bool(payload.get("closed"))}
                return {"success": True, "camera": payload.get(ATTR_CAMERA, body[ATTR_CAMERA])}
            if response.status == 404 and not payload:
                # An app version from before /live_view existed answers its generic 404.
                return {"success": False, "message": "The TV's app is too old for live view; update it"}
            return {
                "success": False,
                "message": payload.get("message") or f"The TV answered HTTP {response.status}",
            }
    except (aiohttp.ClientError, TimeoutError) as err:
        return {"success": False, "message": f"Couldn't reach the TV at {host}:{port} ({err})"}
