"""Test senza Home Assistant: protocollo locale, mappa, cifratura cloud."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.alfred_vacuum import miio
from custom_components.alfred_vacuum.cloud import _rc4
from custom_components.alfred_vacuum.map_decoder import HEADER, decode

@pytest.fixture(autouse=True)
def _allow_udp():
    """I test del protocollo usano un robot finto su 127.0.0.1."""
    try:
        import pytest_socket
    except ImportError:
        yield
        return
    pytest_socket.enable_socket()
    yield


TOKEN = "00112233445566778899aabbccddeeff"


class FakeRobot(asyncio.DatagramProtocol):
    """Robot finto che parla miio come il vero."""

    def __init__(self) -> None:
        tok = bytes.fromhex(TOKEN)
        self.tok = tok
        self.key = miio._md5(tok)
        self.iv = miio._md5(self.key + tok)
        self.received: list[dict] = []

    def connection_made(self, transport) -> None:
        self.transport = transport

    def _crypt(self, data: bytes, enc: bool) -> bytes:
        c = Cipher(algorithms.AES(self.key), modes.CBC(self.iv))
        if enc:
            p = padding.PKCS7(128).padder()
            data = p.update(data) + p.finalize()
            e = c.encryptor()
            return e.update(data) + e.finalize()
        d = c.decryptor()
        raw = d.update(data) + d.finalize()
        u = padding.PKCS7(128).unpadder()
        return u.update(raw) + u.finalize()

    def datagram_received(self, data: bytes, addr) -> None:
        if data == miio.HELLO:
            self.transport.sendto(struct.pack(">HHIII", 0x2131, 32, 0, 0x1234, 1000) + b"\x00" * 16, addr)
            return
        header, checksum, body = data[:16], data[16:32], data[32:]
        assert checksum == miio._md5(header + self.tok + body), "checksum errato"
        msg = json.loads(self._crypt(body, False))
        self.received.append(msg)
        if msg["method"] == "get_properties":
            result = [{"did": p["did"], "siid": p["siid"], "piid": p["piid"], "code": 0, "value": p["siid"] * 100 + p["piid"]} for p in msg["params"]]
        elif msg["method"] == "action":
            result = {"code": 0, "out": [{"piid": 3, "value": "map/obj"}]}
        else:
            result = [{"code": 0}]
        reply = self._crypt(json.dumps({"id": msg["id"], "result": result}).encode(), True)
        head = struct.pack(">HHIII", 0x2131, 32 + len(reply), 0, 0x1234, 1001)
        self.transport.sendto(head + miio._md5(head + self.tok + reply) + reply, addr)


@pytest.fixture
async def robot(monkeypatch):
    loop = asyncio.get_running_loop()
    fake = FakeRobot()
    transport, _ = await loop.create_datagram_endpoint(lambda: fake, local_addr=("127.0.0.1", 0))
    monkeypatch.setattr(miio, "PORT", transport.get_extra_info("sockname")[1])
    yield fake
    transport.close()


async def test_miio_properties_and_action(robot):
    client = miio.MiioClient("127.0.0.1", TOKEN, timeout=2)
    props = [(2, 1), (3, 1)] + [(12, i) for i in range(1, 16)]
    values = await client.get_properties("1", props)
    assert values[(2, 1)] == 201 and values[(3, 1)] == 301 and values[(12, 15)] == 1215
    # 17 proprietà -> 2 richieste da max 14
    assert len([m for m in robot.received if m["method"] == "get_properties"]) == 2
    out = await client.action("1", (4, 7), [{"piid": 10, "value": "{}"}, {"piid": 1, "value": 18}])
    assert out["out"][0]["value"] == "map/obj"
    last = robot.received[-1]
    assert last["params"]["siid"] == 4 and last["params"]["aiid"] == 7


async def test_miio_timeout():
    client = miio.MiioClient("127.0.0.1", TOKEN, timeout=0.2)
    with pytest.raises(miio.MiioError):
        await client.send("get_properties", [])


def _make_map(seg_inf: dict, robot_xy=(150, 50), rism: str | None = None, pixels: bytes | None = None) -> str:
    width, height, grid, left, top = 4, 2, 50, 0, 0
    if pixels is None:
        # riga 0: stanza 1,1,4,4 ; riga 1: stanza 1,1,4,4 (con bit alti impostati)
        pixels = bytes([1, 1, 4 | 0x40, 4, 1, 1, 4, 4 | 0x80])
    header = HEADER.pack(7, 1, ord("I"), robot_xy[0], robot_xy[1], 0, 10, 10, 90, grid, width, height, left, top)
    info: dict = {"seg_inf": seg_inf}
    if rism:
        info = {"rism": rism}
    raw = header + pixels + json.dumps(info).encode()
    return base64.b64encode(zlib.compress(raw)).decode().replace("+", "-").replace("/", "_").rstrip("=")


def test_map_rooms_and_names():
    name = base64.b64encode(b"Cameretta Leo").decode()
    data = decode(_make_map({"1": {"type": 1}, "4": {"type": 4}, "6": {"type": 0, "name": name}}))
    assert data.rooms[1].name == "Soggiorno"
    assert data.rooms[4].name == "Cucina"
    assert data.rooms[6].name == "Cameretta Leo" and data.rooms[6].custom_name
    assert data.robot_room == 4  # x=150 -> colonna 3 -> stanza 4


def test_map_duplicate_types_get_numbers():
    data = decode(_make_map({"2": {"type": 2}, "3": {"type": 2}}))
    assert {r.name for r in data.rooms.values()} == {"Camera da letto 2", "Camera da letto 3"}


def test_map_saved_rism():
    inner = _make_map({"1": {"type": 6}})
    outer = decode(_make_map({}, rism=inner, pixels=bytes(8)))
    assert outer.rooms[1].name == "Bagno"


def test_map_garbage():
    from custom_components.alfred_vacuum.map_decoder import MapDecodeError

    with pytest.raises(MapDecodeError):
        decode("questa-non-e-una-mappa")


def test_rc4_matches_reference():
    from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4

    key = b"0123456789abcdef0123456789abcdef"
    data = b"dati di prova " * 20
    enc = Cipher(ARC4(key), mode=None).encryptor()
    stream = enc.update(b"\x00" * 1024 + data)
    assert _rc4(key, data) == stream[1024:]
    assert _rc4(key, _rc4(key, data)) == data


def test_map_shifted_pixels():
    """Mappa in uso del D10 Plus: pixel = stanza x 4 (verificato il 2026-10-29), 252 = muro."""
    pixels = bytes([4, 4, 16, 16, 4, 252, 16, 17])  # stanza 1, stanza 4, muro, stanza 4 con dettaglio
    data = decode(_make_map({"1": {"type": 4}, "4": {"type": 6}}, robot_xy=(25, 25), pixels=pixels))
    assert data.room_shift == 2
    assert data.robot_room == 1  # colonna 0 -> pixel 4 -> stanza 1 (Cucina), non la 4 (Bagno)
    assert data.room_of(16) == 4 and data.room_of(17) == 4
    assert data.is_wall(252) and data.room_of(252) is None


def test_map_plain_pixels_unchanged():
    data = decode(_make_map({"1": {"type": 1}, "4": {"type": 4}}))
    assert data.room_shift == 0
    assert data.room_of(4 | 0x80) == 4 and data.is_wall(4 | 0x80)
