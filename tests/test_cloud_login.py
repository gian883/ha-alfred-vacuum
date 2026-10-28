"""Login Xiaomi contro un server finto che si comporta come account.xiaomi.com."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.alfred_vacuum import cloud


@pytest.fixture(autouse=True)
def _allow_sockets():
    try:
        import pytest_socket

        pytest_socket.enable_socket()
    except ImportError:
        pass


def _fake_xiaomi(state: dict) -> web.Application:
    app = web.Application()

    def base(request: web.Request) -> str:
        return f"http://{request.host}"

    async def service_login(request):
        return web.Response(text="&&&START&&&" + json.dumps({"code": 70016, "_sign": "abc"}))

    async def auth2(request):
        data = await request.post()
        state["auth2"] = dict(data)
        if state.get("captcha") and data.get("captCode") != "X7Y8":
            return web.Response(text="&&&START&&&" + json.dumps({"code": 87001, "captchaUrl": "/pass/getCode?icodeType=login"}))
        return web.Response(
            text="&&&START&&&" + json.dumps({"code": 0, "notificationUrl": f"{base(request)}/fe/service/identity/authStart?context=CTX1"})
        )

    async def captcha_img(request):
        resp = web.Response(body=b"IMG")
        resp.set_cookie("ick", "ICK1")
        return resp

    async def auth_start(request):
        resp = web.Response(text="<html>ok</html>")
        resp.set_cookie("identity_session", "IDS1")
        return resp

    async def identity_list(request):
        state["list_cookie"] = request.cookies.get("identity_session")
        return web.json_response({"code": 0, "flag": 8, "options": [8]})

    async def send_ticket(request):
        state["sent"] = True
        return web.json_response({"code": 0, "maskedEmail": "g***@gmail.com"})

    async def verify(request):
        data = await request.post()
        state["verify_cookie"] = request.cookies.get("identity_session")
        state["verify_query"] = dict(request.query)
        if request.cookies.get("identity_session") != "IDS1" or data.get("ticket") != "857655":
            return web.json_response({"code": 70014, "tips": "codice errato"})
        return web.json_response({"code": 0, "location": f"{base(request)}/identity/result/check?x=1"})

    async def result_check(request):
        raise web.HTTPFound(f"{base(request)}/pass/serviceLoginAuth2/end?y=2")

    async def auth2_end(request):
        return web.Response(
            status=302,
            headers={
                "extension-pragma": json.dumps({"ssecurity": "c3NlYw==", "psecurity": "p"}),
                "Location": f"{base(request)}/sts?auth=1",
            },
        )

    async def sts(request):
        resp = web.Response(text="ok")
        resp.set_cookie("serviceToken", "SERVICETOKEN")
        resp.set_cookie("userId", "8233")
        return resp

    app.router.add_get("/pass/serviceLogin", service_login)
    app.router.add_post("/pass/serviceLoginAuth2", auth2)
    app.router.add_get("/pass/getCode", captcha_img)
    app.router.add_get("/fe/service/identity/authStart", auth_start)
    app.router.add_get("/identity/list", identity_list)
    app.router.add_post("/identity/auth/sendEmailTicket", send_ticket)
    app.router.add_post("/identity/auth/verifyEmail", verify)
    app.router.add_get("/identity/result/check", result_check)
    app.router.add_get("/pass/serviceLoginAuth2/end", auth2_end)
    app.router.add_get("/sts", sts)
    return app


async def _server(state, monkeypatch):
    server = TestServer(_fake_xiaomi(state))
    await server.start_server()
    monkeypatch.setattr(cloud, "ACCOUNT", str(server.make_url("")).rstrip("/"))
    return server


async def test_login_with_email_code(monkeypatch):
    state: dict = {}
    server = await _server(state, monkeypatch)
    client = cloud.XiaomiCloud("de")
    try:
        challenge = await client.login("user@example.com", "pw")
        assert isinstance(challenge, cloud.TwoFactorChallenge)
        assert challenge.target == "g***@gmail.com" and state["sent"]
        assert state["list_cookie"] == "IDS1"  # il link di verifica è stato aperto prima
        with pytest.raises(cloud.CloudAuthError):
            await client.verify_2fa("000000")
        session = await client.verify_2fa("857655")
        assert session.ssecurity == "c3NlYw==" and session.service_token == "SERVICETOKEN" and session.user_id == "8233"
        assert state["verify_query"]["sid"] == "xiaomiio" and state["verify_query"]["context"] == "CTX1"
        assert "password" not in json.dumps(session.__dict__)
    finally:
        await client.close()
        await server.close()


async def test_login_with_captcha(monkeypatch):
    state: dict = {"captcha": True}
    server = await _server(state, monkeypatch)
    client = cloud.XiaomiCloud("de")
    try:
        with pytest.raises(cloud.CloudCaptchaRequired) as err:
            await client.login("u", "pw")
        assert err.value.image == b"IMG" and not err.value.wrong
        with pytest.raises(cloud.CloudCaptchaRequired) as err:
            await client.login("u", "pw", "sbagliato")
        assert err.value.wrong
        result = await client.login("u", "pw", "X7Y8")
        assert isinstance(result, cloud.TwoFactorChallenge)
        assert state["auth2"]["captCode"] == "X7Y8"
    finally:
        await client.close()
        await server.close()


def _ref_enc_signature(path: str, snonce: str, params: dict) -> str:
    """Formula pubblica della firma (RC4 API): METODO&percorso&k=v...&signed_nonce, SHA1, base64."""
    import base64
    import hashlib

    parts = ["POST", path] + [f"{k}={v}" for k, v in params.items()] + [snonce]
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()


async def test_encrypted_api_and_device_list(monkeypatch):
    import base64
    import hashlib

    ssecurity = base64.b64encode(b"0123456789abcdef").decode()
    seen: list[str] = []

    def snonce_of(nonce: str) -> str:
        return base64.b64encode(hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce)).digest()).decode()

    async def handler(request: web.Request):
        q = dict(request.query)
        snonce = snonce_of(q["_nonce"])
        key = base64.b64decode(snonce)
        path = request.path.replace("/app/", "/", 1)
        enc = {k: q[k] for k in ("data", "rc4_hash__")}
        if q["signature"] != _ref_enc_signature(path, snonce, enc):
            return web.Response(status=400, text="bad signature")
        assert request.cookies.get("serviceToken") == "ST"
        data = json.loads(cloud._rc4(key, base64.b64decode(q["data"])).decode())
        seen.append(path)
        if path == "/v2/homeroom/gethome":
            result = {"homelist": [{"id": "111", "uid": 8233}], "share_home_list": []}
        else:
            assert data["home_id"] == 111 and data["home_owner"] == 8233
            result = {"device_info": [{"did": "999", "model": "dreame.vacuum.r2205", "localip": "192.168.1.9", "token": "ab" * 16}]}
        body = json.dumps({"code": 0, "result": result}).encode()
        return web.Response(text=base64.b64encode(cloud._rc4(key, body)).decode())

    app = web.Application()
    app.router.add_post("/app/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(cloud.XiaomiCloud, "api_base", property(lambda self: str(server.make_url("/app"))))
    client = cloud.XiaomiCloud("de", cloud.CloudSession("8233", "ST", ssecurity, "de"))
    try:
        devices = await client.get_devices()
        assert devices[0]["did"] == "999" and devices[0]["localip"] == "192.168.1.9"
        assert seen == ["/v2/homeroom/gethome", "/v2/home/home_device_list"]
    finally:
        await client.close()
        await server.close()


def test_signature_path():
    assert cloud.signature_path("https://de.api.io.mi.com/app/v2/home/home_device_list") == "/v2/home/home_device_list"
    assert cloud.signature_path("https://api.io.mi.com/app/miotspec/prop/get") == "/miotspec/prop/get"
