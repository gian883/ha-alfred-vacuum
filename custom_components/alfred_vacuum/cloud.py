"""Accesso al cloud Xiaomi: login, elenco dispositivi, chiamate RPC, file mappa.

Il cloud serve per tre cose:
  1. ottenere IP e token del robot (poi i comandi vanno in locale);
  2. scaricare la mappa, che il robot carica sul cloud, per leggere le stanze;
  3. inviare comandi quando il robot non risponde in locale.

La password NON viene salvata: si conservano solo userId, serviceToken e
ssecurity. Quando scadono, Home Assistant chiede di rifare il login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import string
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

ACCOUNT = "https://account.xiaomi.com"
SID = "xiaomiio"
USER_AGENT = (
    "Android-7.1.1-1.0.0-ONEPLUS A3010-136-{agent} APP/xiaomi.smarthome APPV/62830"
)


class CloudError(Exception):
    """Errore generico del cloud."""


class CloudAuthError(CloudError):
    """Credenziali errate o sessione scaduta."""


class CloudCaptchaRequired(CloudError):
    """Xiaomi chiede un captcha: l'immagine va mostrata all'utente."""

    def __init__(self, image: bytes | None, wrong: bool = False) -> None:
        super().__init__("Xiaomi chiede un captcha")
        self.image = image
        self.wrong = wrong


@dataclass
class TwoFactorChallenge:
    """Stato intermedio quando Xiaomi chiede il codice di verifica."""

    notification_url: str
    flag: int = 8  # 4 = SMS, 8 = email
    target: str = ""


@dataclass
class CloudSession:
    user_id: str
    service_token: str
    ssecurity: str
    country: str


def _rand(n: int, chars: str = string.ascii_letters) -> str:
    return "".join(random.choice(chars) for _ in range(n))


def _json(text: str) -> dict[str, Any]:
    return json.loads(text.replace("&&&START&&&", "", 1))


def _rc4(key: bytes, data: bytes) -> bytes:
    """RC4 con scarto dei primi 1024 byte (usato dalle API cifrate Xiaomi)."""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0

    def stream():
        nonlocal i, j
        while True:
            i = (i + 1) % 256
            j = (j + s[i]) % 256
            s[i], s[j] = s[j], s[i]
            yield s[(s[i] + s[j]) % 256]

    gen = stream()
    for _ in range(1024):
        next(gen)
    return bytes(b ^ next(gen) for b in data)


class XiaomiCloud:
    """Client asincrono del cloud Xiaomi."""

    def __init__(self, country: str, session: CloudSession | None = None) -> None:
        self.country = country
        self.session = session
        self._agent_id = _rand(13, "ABCDEF")
        self._device_id = _rand(6, string.ascii_lowercase)
        self._http: aiohttp.ClientSession | None = None
        self._login_state: dict[str, Any] = {}

    # --- utilità HTTP ----------------------------------------------------
    def _client(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                headers={"User-Agent": USER_AGENT.format(agent=self._agent_id)},
                timeout=aiohttp.ClientTimeout(total=20),
            )
            self._http.cookie_jar.update_cookies(
                {"sdkVersion": "accountsdk-18.8.15", "deviceId": self._device_id},
                URL(ACCOUNT),
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    def _cookie(self, name: str) -> str | None:
        for cookie in self._client().cookie_jar:
            if cookie.key == name:
                return cookie.value
        return None

    @property
    def api_base(self) -> str:
        prefix = "" if self.country == "cn" else f"{self.country}."
        return f"https://{prefix}api.io.mi.com/app"

    # --- login -----------------------------------------------------------
    async def _captcha_image(self, url: str) -> bytes | None:
        """Scarica l'immagine del captcha (imposta anche il cookie 'ick')."""
        full = url if url.startswith("http") else f"{ACCOUNT}{url}"
        try:
            async with self._client().get(full) as resp:
                if resp.status == 200:
                    return await resp.read()
        except aiohttp.ClientError as err:
            _LOGGER.debug("Captcha non scaricabile: %s", err)
        return None

    async def login(
        self, username: str, password: str, captcha: str | None = None
    ) -> CloudSession | TwoFactorChallenge:
        """Esegue il login. Può restituire una richiesta di verifica in due passaggi.

        Se Xiaomi chiede un captcha solleva CloudCaptchaRequired con l'immagine:
        la si mostra all'utente e si richiama login() con il testo letto.
        """
        http = self._client()
        async with http.get(f"{ACCOUNT}/pass/serviceLogin", params={"sid": SID, "_json": "true"}) as resp:
            step1 = _json(await resp.text())
        if step1.get("code") == 0 and step1.get("ssecurity"):
            # già autenticato tramite cookie
            return await self._finish(step1)

        data = {
            "sid": SID,
            "hash": hashlib.md5(password.encode()).hexdigest().upper(),
            "callback": "https://sts.api.io.mi.com/sts",
            "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "user": username,
            "_sign": step1.get("_sign", ""),
            "_json": "true",
        }
        if captcha:
            data["captCode"] = captcha.strip()
        async with http.post(f"{ACCOUNT}/pass/serviceLoginAuth2", data=data) as resp:
            step2 = _json(await resp.text())

        if step2.get("captchaUrl"):
            image = await self._captcha_image(step2["captchaUrl"])
            raise CloudCaptchaRequired(image, wrong=bool(captcha))
        if step2.get("notificationUrl"):
            self._login_state = {"username": username, "password": password}
            return await self._start_2fa(step2["notificationUrl"])
        if not step2.get("ssecurity") or not step2.get("location"):
            raise CloudAuthError("Utente o password non corretti")
        return await self._finish(step2)

    async def _finish(self, info: dict[str, Any]) -> CloudSession:
        http = self._client()
        async with http.get(info["location"], allow_redirects=True) as resp:
            await resp.read()
        token = self._cookie("serviceToken")
        if not token:
            raise CloudAuthError("Il cloud non ha restituito il serviceToken")
        self.session = CloudSession(
            user_id=str(info.get("userId")),
            service_token=token,
            ssecurity=info["ssecurity"],
            country=self.country,
        )
        return self.session

    # --- verifica in due passaggi ---------------------------------------
    async def _start_2fa(self, notification_url: str) -> TwoFactorChallenge:
        http = self._client()
        context = parse_qs(urlparse(notification_url).query).get("context", [""])[0]
        self._login_state["context"] = context
        async with http.get(
            f"{ACCOUNT}/identity/list",
            params={"sid": SID, "context": context, "_locale": "it_IT"},
        ) as resp:
            text = await resp.text()
        try:
            flag = int(_json(text).get("flag", 8))
        except (ValueError, TypeError):
            flag = 8
        challenge = TwoFactorChallenge(notification_url=notification_url, flag=flag)
        kind = "Phone" if flag == 4 else "Email"
        async with http.post(
            f"{ACCOUNT}/identity/auth/send{kind}Ticket",
            params={"_dc": str(int(time.time() * 1000)), "sid": SID, "context": context, "mask": "0", "_locale": "it_IT"},
            data={"retry": "0", "icode": "", "_json": "true", "ick": self._cookie("ick") or ""},
        ) as resp:
            sent = _json(await resp.text())
        if sent.get("code") not in (0, None):
            _LOGGER.warning("Invio codice di verifica non riuscito: %s", sent)
        challenge.target = sent.get("maskedEmail") or sent.get("maskedPhone") or ""
        self._login_state["flag"] = flag
        return challenge

    async def verify_2fa(self, code: str) -> CloudSession:
        """Conferma il codice ricevuto via email/SMS e completa il login."""
        http = self._client()
        flag = self._login_state.get("flag", 8)
        kind = "Phone" if flag == 4 else "Email"
        async with http.post(
            f"{ACCOUNT}/identity/auth/verify{kind}",
            params={
                "_flag": str(flag),
                "ticket": code.strip(),
                "trust": "true",
                "_json": "true",
                "context": self._login_state.get("context", ""),
            },
            data={"_flag": str(flag), "ticket": code.strip(), "trust": "true", "_json": "true"},
        ) as resp:
            res = _json(await resp.text())
        if res.get("code") != 0 or not res.get("location"):
            raise CloudAuthError("Codice di verifica non valido")
        async with http.get(res["location"], allow_redirects=True) as resp:
            await resp.read()
        # ora il cookie passToken permette di ottenere ssecurity senza password
        async with http.get(f"{ACCOUNT}/pass/serviceLogin", params={"sid": SID, "_json": "true"}) as resp:
            again = _json(await resp.text())
        if not again.get("ssecurity"):
            # ultimo tentativo con la password, ora che il dispositivo è fidato
            result = await self.login(self._login_state["username"], self._login_state["password"])
            if isinstance(result, TwoFactorChallenge):
                raise CloudAuthError("Xiaomi chiede di nuovo la verifica")
            return result
        return await self._finish(again)

    # --- chiamate firmate -----------------------------------------------
    def _signed_nonce(self, nonce: str) -> str:
        assert self.session
        digest = hashlib.sha256(
            base64.b64decode(self.session.ssecurity) + base64.b64decode(nonce)
        ).digest()
        return base64.b64encode(digest).decode()

    @staticmethod
    def _nonce() -> str:
        millis = int(time.time() * 1000) // 60000
        return base64.b64encode(os.urandom(8) + millis.to_bytes(4, "big")).decode()

    async def api(self, path: str, params: dict[str, Any]) -> Any:
        """Chiamata cifrata RC4 a /app/<path>. Restituisce il campo 'result'."""
        if not self.session:
            raise CloudAuthError("Nessuna sessione cloud")
        url = f"{self.api_base}/{path.lstrip('/')}"
        nonce = self._nonce()
        snonce = self._signed_nonce(nonce)
        key = base64.b64decode(snonce)
        fields = {"data": json.dumps(params, separators=(",", ":"))}

        def enc_signature(values: dict[str, str]) -> str:
            parts = ["POST", url.split("com", 1)[1]]
            parts += [f"{k}={v}" for k, v in values.items()]
            parts.append(snonce)
            return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()

        fields["rc4_hash__"] = enc_signature(fields)
        encrypted = {k: base64.b64encode(_rc4(key, v.encode())).decode() for k, v in fields.items()}
        encrypted["signature"] = enc_signature(encrypted)
        encrypted["ssecurity"] = self.session.ssecurity
        encrypted["_nonce"] = nonce

        headers = {
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }
        cookies = {
            "userId": self.session.user_id,
            "yetAnotherServiceToken": self.session.service_token,
            "serviceToken": self.session.service_token,
            "locale": "it_IT",
            "timezone": "GMT+01:00",
            "is_daylight": "0",
            "dst_offset": "0",
            "channel": "MI_APP_STORE",
        }
        http = self._client()
        async with http.post(url, data=encrypted, headers=headers, cookies=cookies) as resp:
            body = await resp.text()
            status = resp.status
        if status in (401, 403):
            raise CloudAuthError("Sessione cloud scaduta")
        if status != 200:
            raise CloudError(f"HTTP {status} su {path}")
        try:
            plain = _rc4(key, base64.b64decode(body)).decode()
        except (ValueError, UnicodeDecodeError):
            plain = body  # alcune risposte di errore non sono cifrate
        data = json.loads(plain)
        code = data.get("code", 0)
        if code in (2, 3) or "auth err" in str(data.get("message", "")).lower():
            raise CloudAuthError("Sessione cloud scaduta")
        if code != 0:
            raise CloudError(f"{path}: {data.get('message')} (codice {code})")
        return data.get("result")

    # --- funzioni di alto livello ---------------------------------------
    async def get_devices(self) -> list[dict[str, Any]]:
        result = await self.api(
            "home/device_list",
            {"getVirtualModel": True, "getHuamiDevices": 1, "get_split_device": False, "support_smart_home": True},
        )
        return (result or {}).get("list", [])

    async def rpc(self, did: str, method: str, params: Any) -> Any:
        return await self.api(f"home/rpc/{did}", {"method": method, "params": params})

    async def get_properties(self, did: str, props: list[tuple[int, int]]) -> dict[tuple[int, int], Any]:
        result = await self.api(
            "miotspec/prop/get",
            {"params": [{"did": did, "siid": s, "piid": p} for s, p in props]},
        )
        return {
            (r["siid"], r["piid"]): r["value"]
            for r in (result or [])
            if r.get("code", 0) == 0 and "value" in r
        }

    async def set_property(self, did: str, prop: tuple[int, int], value: Any) -> None:
        await self.api(
            "miotspec/prop/set",
            {"params": [{"did": did, "siid": prop[0], "piid": prop[1], "value": value}]},
        )

    async def action(self, did: str, action: tuple[int, int], ins: list[dict] | None = None) -> Any:
        return await self.api(
            "miotspec/action",
            {"params": {"did": did, "siid": action[0], "aiid": action[1], "in": ins or []}},
        )

    async def get_file_url(self, object_name: str) -> str | None:
        """URL temporaneo del file mappa caricato dal robot."""
        result = await self.api("v2/home/get_interim_file_url", {"obj_name": object_name})
        if isinstance(result, dict):
            return result.get("url")
        return None

    async def download(self, url: str) -> bytes:
        async with self._client().get(url) as resp:
            if resp.status != 200:
                raise CloudError(f"Download mappa: HTTP {resp.status}")
            return await resp.read()


def session_signature(session: CloudSession) -> str:
    """Impronta della sessione (per i log, senza esporre i segreti)."""
    return hmac.new(b"alfred", session.service_token.encode(), hashlib.sha256).hexdigest()[:8]
