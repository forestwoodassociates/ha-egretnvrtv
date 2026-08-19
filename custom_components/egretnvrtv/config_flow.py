"""Config flow for the Egret NVR TV integration.

Pairing exchange (see the TV app's NotificationHttpServer.java for the other side):

1. The TV is either auto-discovered via zeroconf (_egretnvrtv._tcp.local.) or entered
   manually (host/port) — the "Connect" screen (zeroconf_confirm/user). Both collect the
   Frigate MQTT topic prefix, whether to subscribe to Frigate's realtime events over this
   connection, and whether to register as a Home Assistant companion app (plus its device
   name if so).
2. The "Alert Appearance" screen (alert_settings) collects default alert position/size/
   duration and whether clips play inline in the popup — split into its own screen rather
   than piled onto the Connect screen, so neither one is an overwhelming wall of fields.
3. The "Notification History" screen (history_settings) collects history retention (save-all
   + how many to keep) — kept separate from alert_settings (rather than combined into one
   six-field screen, as it briefly was) specifically so that if pairing fails right after
   this screen (see below), the error appears on a short two-field form instead of requiring
   a scroll back up past four other fields to see it. Everything the TV's own setup wizard
   would otherwise ask for on-device ends up collected across these three screens.
4. Submitting history_settings is what actually POSTs to the TV's `/ha_pair/start` — which
   makes the TV display a short PIN on-screen and returns its stable device_id/device_name so
   this flow can dedupe/title the entry without trusting zeroconf TXT records (best-effort
   and inconsistent across OEM Android TV builds). Deliberately not triggered any earlier
   (e.g. right after the Connect screen) — every field across all three screens is collected
   *before* the TV is ever contacted, so nothing pops a PIN on the TV's screen until the user
   has answered everything and there's exactly one /ha_pair/complete payload to send, instead
   of an initial one plus a follow-up settings update that could fail on its own.
5. The user reads the PIN off the TV and types it into the form shown here (async_step_pin).
6. This flow mints a fresh Long-Lived Access Token for the instance owner (see
   _async_mint_token below), and POSTs {pin, host, token, mqtt_topic_prefix,
   subscribe_to_frigate_events, register_companion_app, companion_device_name,
   alert_position, alert_size, alert_duration_seconds, play_clips_inline,
   save_all_notifications, history_size} to the TV's `/ha_pair/complete`. The TV only accepts
   this if the PIN matches what it's still showing and hasn't expired — that PIN is the
   entire proof that whoever is submitting this form is physically looking at the right TV,
   since the pairing endpoint itself has no other auth. On success the TV saves the
   host/token immediately (and marks itself, for its own setup wizard's benefit, as answered
   "this TV is on the local network" — this pairing flow only ever works there), then
   (best-effort, non-fatal if it fails) completes its own existing companion-app registration
   using that same token, if asked to.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    ACCESS_TOKEN_LIFESPAN_DAYS,
    ALERT_DURATION_OPTIONS,
    ALERT_POSITION_OPTIONS,
    ALERT_SIZE_OPTIONS,
    CONF_ALERT_DURATION_SECONDS,
    CONF_ALERT_POSITION,
    CONF_ALERT_SIZE,
    CONF_COMPANION_DEVICE_NAME,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_HISTORY_SIZE,
    CONF_MQTT_TOPIC_PREFIX,
    CONF_PLAY_CLIPS_INLINE,
    CONF_REGISTER_COMPANION_APP,
    CONF_SAVE_ALL_NOTIFICATIONS,
    CONF_SUBSCRIBE_TO_FRIGATE_EVENTS,
    CONF_WEBHOOK_ID,
    DEFAULT_ALERT_DURATION_SECONDS,
    DEFAULT_ALERT_POSITION,
    DEFAULT_ALERT_SIZE,
    DEFAULT_COMPANION_DEVICE_NAME_BASE,
    DEFAULT_HISTORY_SIZE,
    DEFAULT_MQTT_TOPIC_PREFIX,
    DEFAULT_PLAY_CLIPS_INLINE,
    DEFAULT_PORT,
    DEFAULT_REGISTER_COMPANION_APP,
    DEFAULT_SAVE_ALL_NOTIFICATIONS,
    DEFAULT_SUBSCRIBE_TO_FRIGATE_EVENTS,
    DOMAIN,
    HISTORY_SIZE_OPTIONS,
    PAIR_COMPLETE_PATH,
    PAIR_START_PATH,
    PAIR_STATUS_PATH,
    PAIR_UPDATE_PATH,
    REQUEST_TIMEOUT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

CONF_PIN = "pin"

ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_INVALID_PIN = "invalid_pin"
ERROR_NO_LOCAL_URL = "no_local_url"
ERROR_OVERLAY_PERMISSION_NOT_GRANTED = "overlay_permission_not_granted"


class EgretNvrTvConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Egret NVR TV."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._device_id: str | None = None
        self._device_name: str | None = None
        self._mqtt_topic_prefix: str = DEFAULT_MQTT_TOPIC_PREFIX
        self._register_companion_app: bool = DEFAULT_REGISTER_COMPANION_APP
        self._companion_device_name: str = ""
        self._subscribe_to_frigate_events: bool = DEFAULT_SUBSCRIBE_TO_FRIGATE_EVENTS
        self._alert_position: str = DEFAULT_ALERT_POSITION
        self._alert_size: str = DEFAULT_ALERT_SIZE
        self._alert_duration_seconds: int = DEFAULT_ALERT_DURATION_SECONDS
        self._play_clips_inline: bool = DEFAULT_PLAY_CLIPS_INLINE
        self._save_all_notifications: bool = DEFAULT_SAVE_ALL_NOTIFICATIONS
        self._history_size: int = DEFAULT_HISTORY_SIZE
        self._webhook_id: str | None = None

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a TV discovered via zeroconf."""
        self._host = discovery_info.host
        self._port = discovery_info.port or DEFAULT_PORT

        # The TV advertises no TXT records (see HaIntegrationAdvertiser's own doc comment —
        # OEM TXT-record support is inconsistent), so there's no reliable id available yet at
        # this point; the real one only comes back from /ha_pair/start, and this step must NOT
        # call that yet — it would pop a pairing PIN on the TV's screen just from being
        # glimpsed over mDNS, before the user has shown any intent to pair. So an already-paired
        # TV is recognized here by host/port match against existing entries' saved data instead
        # of by unique_id (which, for an existing entry, is the *real* device_id set later in
        # _async_start_pairing() — comparing that against this step's host:port-based guess
        # would never match, which is exactly what let an already-configured TV keep re-showing
        # as "newly discovered" on every mDNS re-announce). A coincidentally reused IP after a
        # TV is removed/replaced is the one false-negative this can't catch — low-stakes, and
        # manual "Add Integration" entry still works normally for it.
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_HOST) == self._host and entry.data.get(CONF_PORT) == self._port:
                return self.async_abort(reason="already_configured")

        # Best-effort early dedup for *in-progress* flows (e.g. this same not-yet-configured TV
        # discovered twice before the user acts on either) — properties are whatever the TV's
        # TXT record happened to include, not trusted beyond this.
        early_id = discovery_info.properties.get(CONF_DEVICE_ID) or f"{self._host}:{self._port}"
        await self.async_set_unique_id(str(early_id))
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: self._host, CONF_PORT: self._port}
        )

        self._device_name = discovery_info.properties.get(CONF_DEVICE_NAME) or self._host
        self.context["title_placeholders"] = {"name": self._device_name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm pairing with a zeroconf-discovered TV and collect connection choices."""
        if user_input is not None:
            self._capture_connect_input(user_input)
            return await self.async_step_alert_settings()

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=self._connect_schema(
                {CONF_COMPANION_DEVICE_NAME: self._default_companion_device_name()}
            ),
            description_placeholders={"name": self._device_name or self._host or ""},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual entry, for a TV that wasn't auto-discovered on the network."""
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input[CONF_PORT]
            self._capture_connect_input(user_input)
            return await self.async_step_alert_settings()

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(
                {CONF_COMPANION_DEVICE_NAME: self._default_companion_device_name()}
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update an already-paired TV's settings without re-pairing.

        Deliberately no PIN, unlike the pairing flow above — a PIN's whole purpose is
        proving physical presence at the *first* pairing, when nothing else vouches for
        whoever's submitting the form. Re-demanding that here would just be friction with no
        real security gain: this can only ever reach a TV this Home Assistant instance
        already successfully paired with once (see PAIR_UPDATE_PATH), over the same
        local-network-only listener the rest of this integration already trusts by
        construction — the same trust level /ha_lock_config's own live push already relies
        on. Connection identity (host/port) and the one-shot companion-app registration
        aren't offered here — see this method's own scope: everything else pairing collects.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        is_first_display = user_input is None

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                async with session.post(
                    f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}{PAIR_UPDATE_PATH}",
                    json={
                        CONF_MQTT_TOPIC_PREFIX: user_input[CONF_MQTT_TOPIC_PREFIX],
                        CONF_SUBSCRIBE_TO_FRIGATE_EVENTS: user_input[CONF_SUBSCRIBE_TO_FRIGATE_EVENTS],
                        CONF_ALERT_POSITION: user_input[CONF_ALERT_POSITION],
                        CONF_ALERT_SIZE: user_input[CONF_ALERT_SIZE],
                        CONF_ALERT_DURATION_SECONDS: user_input[CONF_ALERT_DURATION_SECONDS],
                        CONF_PLAY_CLIPS_INLINE: user_input[CONF_PLAY_CLIPS_INLINE],
                        CONF_SAVE_ALL_NOTIFICATIONS: user_input[CONF_SAVE_ALL_NOTIFICATIONS],
                        CONF_HISTORY_SIZE: user_input[CONF_HISTORY_SIZE],
                    },
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                ) as resp:
                    resp.raise_for_status()
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug(
                    "Could not push updated settings to %s:%s: %s",
                    entry.data[CONF_HOST], entry.data[CONF_PORT], err,
                )
                errors["base"] = ERROR_CANNOT_CONNECT
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)

        # Only fetched on the very first display, not on a redisplay-after-error (where
        # user_input already holds exactly what the user just tried, and re-fetching would
        # just throw that away). Prefer the TV's own live values over this entry's cached
        # data.get() equivalent — the two can drift if the TV's settings were changed locally
        # since the last successful pairing/reconfigure.
        defaults = await self._async_fetch_current_status(entry) if is_first_display else None
        if defaults is None:
            defaults = user_input or entry.data

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._reconfigure_schema(defaults),
            errors=errors,
            description_placeholders={
                "name": entry.data.get(CONF_DEVICE_NAME) or entry.title
            },
        )

    async def _async_fetch_current_status(self, entry: ConfigEntry) -> dict[str, Any] | None:
        """GETs the TV's own live settings (see NotificationHttpServer's /ha_pair/status
        route) — best-effort; returns None on any failure so the caller falls back to this
        entry's own cached data instead."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}{PAIR_STATUS_PATH}",
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug(
                "Could not fetch current status from %s:%s: %s",
                entry.data[CONF_HOST], entry.data[CONF_PORT], err,
            )
            return None

    def _capture_connect_input(self, user_input: dict[str, Any]) -> None:
        self._mqtt_topic_prefix = user_input[CONF_MQTT_TOPIC_PREFIX]
        self._subscribe_to_frigate_events = user_input[CONF_SUBSCRIBE_TO_FRIGATE_EVENTS]
        self._register_companion_app = user_input[CONF_REGISTER_COMPANION_APP]
        self._companion_device_name = user_input.get(
            CONF_COMPANION_DEVICE_NAME, ""
        ) or self._default_companion_device_name()

    async def async_step_alert_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect alert appearance settings, then move on to history settings."""
        if user_input is not None:
            self._alert_position = user_input[CONF_ALERT_POSITION]
            self._alert_size = user_input[CONF_ALERT_SIZE]
            self._alert_duration_seconds = user_input[CONF_ALERT_DURATION_SECONDS]
            self._play_clips_inline = user_input[CONF_PLAY_CLIPS_INLINE]
            return await self.async_step_history_settings()

        return self.async_show_form(
            step_id="alert_settings",
            data_schema=self._alert_settings_schema(user_input),
            description_placeholders={"name": self._device_name or self._host or ""},
        )

    async def async_step_history_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect history settings, then start pairing.

        Submitting this step (not either screen before it) is what actually contacts the TV
        — see this module's own doc comment for why that's deliberate, and why history
        settings specifically are what this is attached to rather than alert_settings.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            self._save_all_notifications = user_input[CONF_SAVE_ALL_NOTIFICATIONS]
            self._history_size = user_input[CONF_HISTORY_SIZE]

            errors = await self._async_start_pairing()
            if not errors:
                return await self.async_step_pin()

        return self.async_show_form(
            step_id="history_settings",
            data_schema=self._history_settings_schema(user_input),
            errors=errors,
            description_placeholders={"name": self._device_name or self._host or ""},
        )

    @staticmethod
    def _connect_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
        defaults = defaults or {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_MQTT_TOPIC_PREFIX,
                    default=defaults.get(CONF_MQTT_TOPIC_PREFIX, DEFAULT_MQTT_TOPIC_PREFIX),
                ): str,
                # Leave unchecked if a "Notifications for Android TV" blueprint is already
                # pushing to this TV — enabling both shows every event twice, same warning the
                # TV's own Home Assistant settings card gives for this exact checkbox.
                vol.Required(
                    CONF_SUBSCRIBE_TO_FRIGATE_EVENTS,
                    default=defaults.get(
                        CONF_SUBSCRIBE_TO_FRIGATE_EVENTS, DEFAULT_SUBSCRIBE_TO_FRIGATE_EVENTS
                    ),
                ): bool,
                vol.Required(
                    CONF_REGISTER_COMPANION_APP,
                    default=defaults.get(
                        CONF_REGISTER_COMPANION_APP, DEFAULT_REGISTER_COMPANION_APP
                    ),
                ): bool,
                # Only used when the checkbox above is on — shown as the device/notify target
                # name in Home Assistant, same "device name" the TV's own setup wizard asks
                # for when registering as a companion app.
                vol.Optional(
                    CONF_COMPANION_DEVICE_NAME,
                    default=defaults.get(CONF_COMPANION_DEVICE_NAME, ""),
                ): str,
            }
        )

    @staticmethod
    def _alert_settings_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
        defaults = defaults or {}
        return vol.Schema(
            {
                # Alert appearance — same options as the TV's own "Alert Configuration" card,
                # collected here so there's no follow-up trip to Settings after pairing.
                vol.Required(
                    CONF_ALERT_POSITION,
                    default=defaults.get(CONF_ALERT_POSITION, DEFAULT_ALERT_POSITION),
                ): vol.In(ALERT_POSITION_OPTIONS),
                vol.Required(
                    CONF_ALERT_SIZE,
                    default=defaults.get(CONF_ALERT_SIZE, DEFAULT_ALERT_SIZE),
                ): vol.In(ALERT_SIZE_OPTIONS),
                # vol.Coerce(int) first: a plain vol.In() over an int list, with no explicit
                # selector, can round-trip through the frontend as a string — harmless for the
                # TV (org.json's optInt() parses a numeric string just fine) but it means the
                # *stored* value in this entry's data could be a str, and vol.In() itself
                # doesn't coerce, only validates membership. That silently breaks pre-selecting
                # the right option next time this same form (e.g. Reconfigure) is redisplayed
                # with that stored value as the default — "20" != 20, so nothing matches.
                # Coercing first guarantees an int actually gets stored, so redisplay works.
                vol.Required(
                    CONF_ALERT_DURATION_SECONDS,
                    default=defaults.get(
                        CONF_ALERT_DURATION_SECONDS, DEFAULT_ALERT_DURATION_SECONDS
                    ),
                ): vol.All(vol.Coerce(int), vol.In(ALERT_DURATION_OPTIONS)),
                vol.Required(
                    CONF_PLAY_CLIPS_INLINE,
                    default=defaults.get(CONF_PLAY_CLIPS_INLINE, DEFAULT_PLAY_CLIPS_INLINE),
                ): bool,
            }
        )

    @staticmethod
    def _history_settings_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
        defaults = defaults or {}
        return vol.Schema(
            {
                # Same options as the TV's own History card.
                vol.Required(
                    CONF_SAVE_ALL_NOTIFICATIONS,
                    default=defaults.get(
                        CONF_SAVE_ALL_NOTIFICATIONS, DEFAULT_SAVE_ALL_NOTIFICATIONS
                    ),
                ): bool,
                # See CONF_ALERT_DURATION_SECONDS's own comment above for why vol.Coerce(int)
                # comes first.
                vol.Required(
                    CONF_HISTORY_SIZE,
                    default=defaults.get(CONF_HISTORY_SIZE, DEFAULT_HISTORY_SIZE),
                ): vol.All(vol.Coerce(int), vol.In(HISTORY_SIZE_OPTIONS)),
            }
        )

    @classmethod
    def _reconfigure_schema(cls, defaults: dict[str, Any] | None = None) -> vol.Schema:
        # One combined screen, unlike the alert_settings/history_settings split above — that
        # split exists specifically so a pairing error doesn't require scrolling past a wall
        # of fields to see, which doesn't apply here (no PIN step, and any /ha_pair/update
        # failure shows on this same, only, screen regardless of its length).
        defaults = defaults or {}
        schema = {
            vol.Required(
                CONF_MQTT_TOPIC_PREFIX,
                default=defaults.get(CONF_MQTT_TOPIC_PREFIX, DEFAULT_MQTT_TOPIC_PREFIX),
            ): str,
            vol.Required(
                CONF_SUBSCRIBE_TO_FRIGATE_EVENTS,
                default=defaults.get(
                    CONF_SUBSCRIBE_TO_FRIGATE_EVENTS, DEFAULT_SUBSCRIBE_TO_FRIGATE_EVENTS
                ),
            ): bool,
        }
        schema.update(cls._alert_settings_schema(defaults).schema)
        schema.update(cls._history_settings_schema(defaults).schema)
        return vol.Schema(schema)

    @classmethod
    def _user_schema(cls, defaults: dict[str, Any] | None = None) -> vol.Schema:
        defaults = defaults or {}
        schema = {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
        }
        schema.update(cls._connect_schema(defaults).schema)
        return vol.Schema(schema)

    def _default_companion_device_name(self) -> str:
        """Suggests "Egret NVR TV", or "Egret NVR TV 2"/"3"/... if that name is already
        used by another TV paired through this integration — so two TVs don't silently
        default to registering as the same Home Assistant companion device."""
        existing_names = {
            entry.data.get(CONF_COMPANION_DEVICE_NAME)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_COMPANION_DEVICE_NAME)
        }
        if DEFAULT_COMPANION_DEVICE_NAME_BASE not in existing_names:
            return DEFAULT_COMPANION_DEVICE_NAME_BASE
        suffix = 2
        while f"{DEFAULT_COMPANION_DEVICE_NAME_BASE} {suffix}" in existing_names:
            suffix += 1
        return f"{DEFAULT_COMPANION_DEVICE_NAME_BASE} {suffix}"

    async def _async_start_pairing(self) -> dict[str, str]:
        """POST /ha_pair/start and learn the TV's real identity. Returns a form errors dict
        (empty on success) — the caller (async_step_alert_settings) decides what to do with it,
        same pattern _async_finish_pairing()/async_step_pin() already use below."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"http://{self._host}:{self._port}{PAIR_START_PATH}",
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Could not reach TV at %s:%s: %s", self._host, self._port, err)
            return {"base": ERROR_CANNOT_CONNECT}

        # The TV already generated and is holding a real PIN at this point, but its own
        # overlay window (the only place it could show one) silently does nothing without
        # this permission — proceeding to the "enter the code" step would just leave the user
        # stuck looking at a TV with nothing on screen to read. Defaults to True for a TV
        # running an older build that doesn't send this field yet, so pairing isn't blocked on
        # a check that build simply can't answer.
        if not data.get("overlay_permission_granted", True):
            return {"base": ERROR_OVERLAY_PERMISSION_NOT_GRANTED}

        self._device_id = str(data.get(CONF_DEVICE_ID) or f"{self._host}:{self._port}")
        self._device_name = str(data.get(CONF_DEVICE_NAME) or self._device_name or self._host)

        await self.async_set_unique_id(self._device_id)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: self._host, CONF_PORT: self._port}
        )

        return {}

    async def async_step_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the PIN shown on the TV, then finish pairing."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_finish_pairing(user_input[CONF_PIN])
            if not errors:
                return self.async_create_entry(
                    title=self._device_name or self._host or "Egret NVR TV",
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_DEVICE_ID: self._device_id,
                        CONF_DEVICE_NAME: self._device_name,
                        CONF_WEBHOOK_ID: self._webhook_id,
                        CONF_MQTT_TOPIC_PREFIX: self._mqtt_topic_prefix,
                        CONF_SUBSCRIBE_TO_FRIGATE_EVENTS: self._subscribe_to_frigate_events,
                        CONF_REGISTER_COMPANION_APP: self._register_companion_app,
                        # Stored so _default_companion_device_name() can steer a later
                        # pairing (a second TV) away from reusing this same name.
                        CONF_COMPANION_DEVICE_NAME: self._companion_device_name
                        if self._register_companion_app
                        else None,
                        CONF_ALERT_POSITION: self._alert_position,
                        CONF_ALERT_SIZE: self._alert_size,
                        CONF_ALERT_DURATION_SECONDS: self._alert_duration_seconds,
                        CONF_PLAY_CLIPS_INLINE: self._play_clips_inline,
                        CONF_SAVE_ALL_NOTIFICATIONS: self._save_all_notifications,
                        CONF_HISTORY_SIZE: self._history_size,
                    },
                )
            if errors.get("base") == ERROR_INVALID_PIN:
                # The PIN the TV was showing is now spent/expired — ask it to display a fresh
                # one rather than leave the user stuck retrying a code that can never work.
                await self._async_request_new_pin()

        return self.async_show_form(
            step_id="pin",
            data_schema=vol.Schema({vol.Required(CONF_PIN): str}),
            errors=errors,
            description_placeholders={"name": self._device_name or self._host or ""},
        )

    async def _async_request_new_pin(self) -> None:
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"http://{self._host}:{self._port}{PAIR_START_PATH}",
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ):
                pass
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Could not request a fresh PIN: %s", err)

    async def _async_finish_pairing(self, pin: str) -> dict[str, str]:
        """POST /ha_pair/complete with a freshly minted token. Returns a form errors dict."""
        try:
            local_url = get_url(self.hass, allow_external=False, prefer_external=False)
        except NoURLAvailableError:
            return {"base": ERROR_NO_LOCAL_URL}

        try:
            token = await self._async_mint_token()
        except ValueError as err:
            _LOGGER.error("Could not create a Home Assistant access token for the TV: %s", err)
            return {"base": ERROR_CANNOT_CONNECT}

        # Generated once and reused across retries (e.g. a mistyped PIN redisplaying this
        # step) rather than freshly per attempt — nothing yet holds a stale copy of an earlier
        # id to clean up, since async_setup_entry() is what actually registers a webhook_id
        # with Home Assistant, and that only ever runs once pairing has fully succeeded.
        if self._webhook_id is None:
            self._webhook_id = webhook.async_generate_id()

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"http://{self._host}:{self._port}{PAIR_COMPLETE_PATH}",
                json={
                    "pin": pin,
                    "host": local_url,
                    "token": token,
                    "webhook_id": self._webhook_id,
                    "mqtt_topic_prefix": self._mqtt_topic_prefix,
                    "subscribe_to_frigate_events": self._subscribe_to_frigate_events,
                    "register_companion_app": self._register_companion_app,
                    "companion_device_name": self._companion_device_name,
                    "alert_position": self._alert_position,
                    "alert_size": self._alert_size,
                    "alert_duration_seconds": self._alert_duration_seconds,
                    "play_clips_inline": self._play_clips_inline,
                    "save_all_notifications": self._save_all_notifications,
                    "history_size": self._history_size,
                },
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 403:
                    return {"base": ERROR_INVALID_PIN}
                resp.raise_for_status()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Could not complete pairing with %s:%s: %s", self._host, self._port, err)
            return {"base": ERROR_CANNOT_CONNECT}

        return {}

    async def _async_mint_token(self) -> str:
        """Create a Long-Lived Access Token for the instance owner, for this TV to use.

        Mirrors exactly what Home Assistant's own "auth/long_lived_access_token" WebSocket
        command does for a token created by hand via the profile page (see
        homeassistant/components/auth/__init__.py) — a refresh token of the long-lived type,
        then an access token string derived from it. There's no "current user" available
        inside a config flow (only the frontend's live WebSocket connection carries that), so
        this attributes the token to the instance's owner account instead — config flows are
        already admin-only (see Home Assistant's own @require_admin guard on starting one),
        so that's the same trust level the person completing this flow already has.
        """
        owner = await self.hass.auth.async_get_owner()
        if owner is None:
            raise ValueError("This Home Assistant instance has no owner account")

        client_name = f"Egret NVR TV ({self._device_id})"

        # Re-pairing the same TV (e.g. after a factory reset or reinstall) would otherwise hit
        # "{client_name} already exists" the second time around — revoke the old token first
        # so re-pairing cleanly replaces it instead of failing, and so Home Assistant's own
        # token list doesn't accumulate stale entries for the same TV.
        for existing in list(owner.refresh_tokens.values()):
            if (
                existing.client_name == client_name
                and existing.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
            ):
                self.hass.auth.async_remove_refresh_token(existing)

        refresh_token = await self.hass.auth.async_create_refresh_token(
            owner,
            client_name=client_name,
            token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
            access_token_expiration=timedelta(days=ACCESS_TOKEN_LIFESPAN_DAYS),
        )
        return self.hass.auth.async_create_access_token(refresh_token)
