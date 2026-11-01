"""Client minimo del protocollo locale miio (UDP 54321).

Formato del pacchetto (32 byte di intestazione + dati cifrati):
  0x2131 | lunghezza (2) | 0 (4) | id dispositivo (4) | timestamp (4) | md5 (16)
Cifratura: AES-128-CBC, chiave = md5(token), iv = md5(chiave + token), padding PKCS7.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import struct
import time
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

PORT = 54321
HELLO = bytes.fromhex("21310020" + "ff" * 28)


class MiioError(Exception):
    """Errore di comunicazione locale."""


class MiioTimeout(MiioError):
    """Il robot non ha risposto."""


class MiioDeviceError(MiioError):
    """Il robot ha risposto con un errore."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.code = error.get("code")


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.queue.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Errore UDP: %s", exc)


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Raccoglie le risposte al saluto: {id dispositivo: indirizzo IP}."""

    def __init__(self, wanted: int) -> None:
        self.wanted = wanted
        self.found: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr: Any) -> None:
        if len(data) < 32 or data[:2] != b"\x21\x31" or self.found.done():
            return
        device_id = struct.unpack(">I", data[8:12])[0]
        if device_id == self.wanted:
            self.found.set_result(addr[0])

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Errore UDP nella ricerca: %s", exc)


async def discover(did: str | int, near: str | None = None, timeout: float = 4.0) -> str | None:
    """Cerca il robot sulla rete locale e ne restituisce l'IP (None se non risponde).

    Tutti i dispositivi miio rispondono al pacchetto di saluto con il proprio id:
    lo mando in broadcast e, per le reti che filtrano il broadcast (mesh, Wi-Fi),
    anche a ogni indirizzo della sottorete /24 dell'ultimo IP noto.
    Nessun cloud, nessun login: solo rete di casa.
    """
    try:
        wanted = int(did)
    except (TypeError, ValueError):
        return None
    loop = asyncio.get_running_loop()
    try:
        transport, proto = await loop.create_datagram_endpoint(
            lambda: _DiscoveryProtocol(wanted), local_addr=("0.0.0.0", 0), allow_broadcast=True
        )
    except OSError as err:
        _LOGGER.debug("Ricerca del robot non avviabile: %s", err)
        return None
    try:
        targets = ["255.255.255.255"]
        if near:
            try:
                net = ipaddress.ip_network(f"{near}/24", strict=False)
                targets += [str(ip) for ip in net.hosts()]
            except ValueError:
                pass
        for _round in range(2):  # due giri: un pacchetto UDP perso non basta a mancarlo
            for target in targets:
                try:
                    transport.sendto(HELLO, (target, PORT))
                except OSError:
                    continue
            try:
                return await asyncio.wait_for(asyncio.shield(proto.found), timeout / 2)
            except TimeoutError:
                continue
        return None
    finally:
        transport.close()


class MiioClient:
    """Invia comandi JSON al robot sulla rete locale."""

    def __init__(self, host: str, token: str, timeout: float = 5.0) -> None:
        self.host = host
        self._token = bytes.fromhex(token)
        self._key = _md5(self._token)
        self._iv = _md5(self._key + self._token)
        self._timeout = timeout
        self._device_id = 0
        self._stamp = 0
        self._stamp_at = 0.0
        self._msg_id = int(time.time()) % 10000
        self._lock = asyncio.Lock()

    def set_host(self, host: str) -> None:
        """Nuovo indirizzo del robot: la sessione va rifatta con un nuovo saluto."""
        self.host = host
        self._device_id = 0

    # --- cifratura -------------------------------------------------------
    def _encrypt(self, plain: bytes) -> bytes:
        padder = padding.PKCS7(128).padder()
        data = padder.update(plain) + padder.finalize()
        enc = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).encryptor()
        return enc.update(data) + enc.finalize()

    def _decrypt(self, data: bytes) -> bytes:
        dec = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).decryptor()
        plain = dec.update(data) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(plain) + unpadder.finalize()

    def _build(self, payload: bytes) -> bytes:
        encrypted = self._encrypt(payload)
        stamp = self._stamp + int(time.monotonic() - self._stamp_at)
        header = struct.pack(">HHIII", 0x2131, 32 + len(encrypted), 0, self._device_id, stamp)
        checksum = _md5(header + self._token + encrypted)
        return header + checksum + encrypted

    # --- trasporto -------------------------------------------------------
    async def _exchange(self, packet: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        transport, proto = await loop.create_datagram_endpoint(
            _Protocol, remote_addr=(self.host, PORT)
        )
        try:
            transport.sendto(packet)
            return await asyncio.wait_for(proto.queue.get(), self._timeout)
        except TimeoutError as err:
            raise MiioTimeout(f"Nessuna risposta da {self.host}") from err
        finally:
            transport.close()

    async def _handshake(self) -> None:
        reply = await self._exchange(HELLO)
        if len(reply) < 32:
            raise MiioError("Risposta di saluto non valida")
        _, _, _, self._device_id, self._stamp = struct.unpack(">HHIII", reply[:16])
        self._stamp_at = time.monotonic()

    async def send(self, method: str, params: Any = None) -> Any:
        """Invia un comando e restituisce il campo 'result'."""
        async with self._lock:
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    if not self._device_id or attempt:
                        await self._handshake()
                    self._msg_id = (self._msg_id + 1) % 100000 or 1
                    msg_id = self._msg_id
                    body = json.dumps(
                        {"id": msg_id, "method": method, "params": params if params is not None else []},
                        separators=(",", ":"),
                    ).encode()
                    reply = await self._exchange(self._build(body))
                    if len(reply) <= 32:
                        raise MiioError("Risposta vuota")
                    text = self._decrypt(reply[32:]).rstrip(b"\x00").decode("utf-8", "replace")
                    data = json.loads(text)
                    if data.get("id") != msg_id:
                        raise MiioError("Risposta a un'altra richiesta")
                    if "error" in data:
                        raise MiioDeviceError(data["error"])
                    return data.get("result")
                except MiioDeviceError:
                    raise
                except (MiioError, ValueError, OSError) as err:
                    last_err = err
                    _LOGGER.debug("Tentativo %s fallito verso %s: %s", attempt + 1, self.host, err)
            raise last_err if isinstance(last_err, MiioError) else MiioError(str(last_err))

    # --- comandi MIoT ----------------------------------------------------
    async def get_properties(self, did: str, props: list[tuple[int, int]]) -> dict[tuple[int, int], Any]:
        """Legge le proprietà indicate; restituisce {(siid, piid): valore}."""
        values: dict[tuple[int, int], Any] = {}
        # il robot accetta al massimo ~15 proprietà per richiesta
        for start in range(0, len(props), 14):
            chunk = props[start : start + 14]
            params = [{"did": f"{s}-{p}", "siid": s, "piid": p} for s, p in chunk]
            result = await self.send("get_properties", params) or []
            for item in result:
                if item.get("code", 0) == 0 and "value" in item:
                    values[(item["siid"], item["piid"])] = item["value"]
        return values

    async def set_property(self, did: str, prop: tuple[int, int], value: Any) -> None:
        siid, piid = prop
        result = await self.send(
            "set_properties", [{"did": f"set-{siid}-{piid}", "siid": siid, "piid": piid, "value": value}]
        )
        code = (result or [{}])[0].get("code", 0)
        if code not in (0, 1):
            raise MiioDeviceError({"code": code, "message": "set_properties rifiutato"})

    async def action(self, did: str, action: tuple[int, int], ins: list[dict] | None = None) -> Any:
        siid, aiid = action
        result = await self.send(
            "action", {"did": f"call-{siid}-{aiid}", "siid": siid, "aiid": aiid, "in": ins or []}
        )
        if isinstance(result, dict):
            code = result.get("code", 0)
            if code not in (0, 1):
                raise MiioDeviceError({"code": code, "message": "azione rifiutata"})
        return result
