"""Configurazione da interfaccia."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .cloud import CloudAuthError, CloudCaptchaRequired, CloudError, CloudSession, TwoFactorChallenge, XiaomiCloud
from .const import (
    CONF_CLOUD,
    CONF_COUNTRY,
    CONF_DID,
    CONF_MAP_INTERVAL,
    CONF_ROOM_NAMES,
    CONF_SCAN_INTERVAL,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_MAP_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MODEL,
    P_STATUS,
)
from .miio import MiioClient, MiioError

_LOGGER = logging.getLogger(__name__)

COUNTRY_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=COUNTRIES, mode=SelectSelectorMode.DROPDOWN, translation_key="country")
)
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

CAPTCHA_KEY = f"{DOMAIN}_captcha"


class CaptchaView(HomeAssistantView):
    """Serve l'immagine del captcha a un indirizzo casuale e temporaneo."""

    url = f"/api/{DOMAIN}/captcha/{{key}}"
    name = f"api:{DOMAIN}:captcha"
    requires_auth = False  # l'indirizzo è un segreto casuale usato una volta sola

    async def get(self, request: web.Request, key: str) -> web.Response:
        images: dict[str, bytes] = request.app[KEY_HASS].data.get(CAPTCHA_KEY, {})
        image = images.get(key)
        if image is None:
            return web.Response(status=404)
        return web.Response(body=image, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _publish_captcha(hass, image: bytes) -> str:
    images: dict[str, bytes] = hass.data.setdefault(CAPTCHA_KEY, {})
    if not hass.data.get(f"{CAPTCHA_KEY}_view"):
        hass.http.register_view(CaptchaView())
        hass.data[f"{CAPTCHA_KEY}_view"] = True
    images.clear()  # tengo solo l'ultimo
    key = secrets.token_urlsafe(24)
    images[key] = image
    return f"/api/{DOMAIN}/captcha/{key}"


class AlfredConfigFlow(ConfigFlow, domain=DOMAIN):
    """Login Xiaomi oppure IP + token."""

    VERSION = 1

    def __init__(self) -> None:
        self._cloud: XiaomiCloud | None = None
        self._session: CloudSession | None = None
        self._challenge: TwoFactorChallenge | None = None
        self._devices: list[dict[str, Any]] = []
        self._country = DEFAULT_COUNTRY
        self._credentials: tuple[str, str] = ("", "")  # solo in memoria, durante la procedura
        self._captcha_url = ""
        self._captcha_wrong = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AlfredOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["cloud", "manual"])

    # --- account Xiaomi --------------------------------------------------
    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._country = user_input[CONF_COUNTRY]
            result = await self._login(user_input[CONF_USERNAME], user_input[CONF_PASSWORD], errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): PASSWORD,
                    vol.Required(CONF_COUNTRY, default=self._country): COUNTRY_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def _login(
        self, username: str, password: str, errors: dict[str, str], captcha: str | None = None
    ) -> ConfigFlowResult | None:
        if captcha is None:
            # nuovo tentativo da zero; con il captcha si riusa la stessa sessione (cookie)
            if self._cloud:
                await self._cloud.close()
            self._cloud = XiaomiCloud(self._country)
        assert self._cloud
        self._credentials = (username, password)
        try:
            result = await self._cloud.login(username, password, captcha)
        except CloudCaptchaRequired as err:
            if not err.image:
                errors["base"] = "captcha"
                return None
            self._captcha_url = _publish_captcha(self.hass, err.image)
            self._captcha_wrong = err.wrong
            return await self.async_step_captcha()
        except CloudAuthError:
            errors["base"] = "invalid_auth"
            return None
        except (CloudError, aiohttp.ClientError, ValueError) as err:
            _LOGGER.warning("Login Xiaomi non riuscito: %s", err)
            errors["base"] = "cannot_connect"
            return None
        if isinstance(result, TwoFactorChallenge):
            self._challenge = result
            return await self.async_step_verify()
        self._session = result
        return await self._after_login()

    async def async_step_captcha(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Mostra l'immagine del captcha e chiede il testo."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username, password = self._credentials
            result = await self._login(username, password, errors, captcha=user_input["captcha"])
            if result is not None:
                return result
        elif self._captcha_wrong:
            errors["base"] = "captcha_wrong"
        return self.async_show_form(
            step_id="captcha",
            data_schema=vol.Schema({vol.Required("captcha"): str}),
            description_placeholders={"image": f"![captcha]({self._captcha_url})"},
            errors=errors,
        )

    async def async_step_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._cloud:
            try:
                self._session = await self._cloud.verify_2fa(user_input["code"])
            except CloudAuthError:
                errors["base"] = "invalid_code"
            except (CloudError, aiohttp.ClientError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                return await self._after_login()
        target = self._challenge.target if self._challenge else ""
        return self.async_show_form(
            step_id="verify",
            data_schema=vol.Schema({vol.Required("code"): str}),
            description_placeholders={"target": target or "email/telefono dell'account"},
            errors=errors,
        )

    async def _after_login(self) -> ConfigFlowResult:
        self.hass.data.get(CAPTCHA_KEY, {}).clear()
        self._credentials = ("", "")
        if self.source == "reauth":
            return await self._finish_reauth()
        return await self.async_step_device()

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if not self._devices:
            assert self._cloud
            try:
                devices = await self._cloud.get_devices()
            except (CloudError, aiohttp.ClientError, ValueError) as err:
                _LOGGER.warning("Elenco dispositivi non disponibile: %s", err)
                return self.async_abort(reason="cannot_connect")
            self._devices = [d for d in devices if d.get("model") == MODEL]
            if not self._devices:
                return self.async_abort(reason="no_devices")

        if user_input is None and len(self._devices) == 1:
            user_input = {CONF_DID: str(self._devices[0]["did"])}
        if user_input is not None:
            device = next(d for d in self._devices if str(d["did"]) == user_input[CONF_DID])
            await self.async_set_unique_id(str(device["did"]))
            self._abort_if_unique_id_configured()
            assert self._session
            return self.async_create_entry(
                title=device.get("name") or "Dreame D10 Plus",
                data={
                    CONF_DID: str(device["did"]),
                    CONF_HOST: device.get("localip"),
                    CONF_TOKEN: device.get("token"),
                    CONF_CLOUD: True,
                    CONF_COUNTRY: self._country,
                    CONF_USER_ID: self._session.user_id,
                    CONF_SERVICE_TOKEN: self._session.service_token,
                    CONF_SSECURITY: self._session.ssecurity,
                },
            )
        options = {str(d["did"]): f"{d.get('name')} ({d.get('localip')})" for d in self._devices}
        return self.async_show_form(
            step_id="device", data_schema=vol.Schema({vol.Required(CONF_DID): vol.In(options)}), errors=errors
        )

    # --- solo rete locale ------------------------------------------------
    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{32}", token):
                errors[CONF_TOKEN] = "invalid_token"
            else:
                client = MiioClient(user_input[CONF_HOST], token)
                try:
                    await client.get_properties("0", [P_STATUS])
                except MiioError:
                    errors["base"] = "cannot_connect"
                else:
                    did = user_input.get(CONF_DID) or user_input[CONF_HOST]
                    await self.async_set_unique_id(str(did))
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=user_input.get(CONF_NAME) or "Dreame D10 Plus",
                        data={CONF_DID: str(did), CONF_HOST: user_input[CONF_HOST], CONF_TOKEN: token, CONF_CLOUD: False},
                    )
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_TOKEN): PASSWORD,
                    vol.Optional(CONF_DID): str,
                    vol.Optional(CONF_NAME, default="ALFRED"): str,
                }
            ),
            errors=errors,
        )

    # --- nuovo login quando la sessione scade ----------------------------
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._country = entry_data.get(CONF_COUNTRY, DEFAULT_COUNTRY)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._login(user_input[CONF_USERNAME], user_input[CONF_PASSWORD], errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): PASSWORD}),
            errors=errors,
        )

    async def _finish_reauth(self) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        assert self._session and self._cloud
        updates: dict[str, Any] = {
            CONF_USER_ID: self._session.user_id,
            CONF_SERVICE_TOKEN: self._session.service_token,
            CONF_SSECURITY: self._session.ssecurity,
        }
        # aggiorno anche IP e token se sono cambiati
        try:
            for device in await self._cloud.get_devices():
                if str(device.get("did")) == str(entry.data[CONF_DID]):
                    updates[CONF_HOST] = device.get("localip") or entry.data.get(CONF_HOST)
                    updates[CONF_TOKEN] = device.get("token") or entry.data.get(CONF_TOKEN)
        except (CloudError, aiohttp.ClientError, ValueError):
            pass
        await self._cloud.close()
        return self.async_update_reload_and_abort(entry, data_updates=updates)


class AlfredOptionsFlow(OptionsFlow):
    """Nomi stanze personalizzati e frequenze di aggiornamento."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ROOM_NAMES, default=options.get(CONF_ROOM_NAMES, "")): str,
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
                    vol.Required(
                        CONF_MAP_INTERVAL, default=options.get(CONF_MAP_INTERVAL, DEFAULT_MAP_INTERVAL)
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=1440)),
                }
            ),
        )
