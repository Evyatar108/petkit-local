"""The phone app's gateway check, `GET /api/fountain/auth/check`.

Tokens are minted here with a locally generated RSA key and a self-signed
certificate, served to the check by a local stand-in for Google's certificate
endpoint; nothing reaches Google.
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.fountain_app import auth
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app

PROJECT = "fountain-test"
EMAIL = "owner@example.com"
KID = "kid-1"
CHECK = "/api/fountain/auth/check"


def _cert_pem(key: rsa.RSAPrivateKey) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "securetoken.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture(scope="module")
def keys():
    return {name: rsa.generate_private_key(public_exponent=65537, key_size=2048)
            for name in ("google", "other")}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint(key: rsa.RSAPrivateKey, kid: str = KID, header: dict | None = None,
         **claims) -> str:
    now = int(time.time())
    body = {"iss": f"https://securetoken.google.com/{PROJECT}", "aud": PROJECT,
            "auth_time": now - 60, "user_id": "uid-1", "sub": "uid-1",
            "iat": now - 10, "exp": now + 3600, "email": EMAIL, "email_verified": True,
            "firebase": {"sign_in_provider": "google.com"}}
    body.update(claims)
    head = {"alg": "RS256", "kid": kid, "typ": "JWT", **(header or {})}
    signed = f"{_b64(json.dumps(head).encode())}.{_b64(json.dumps(body).encode())}"
    sig = key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signed}.{_b64(sig)}"


class CertServer:
    """A local stand-in for Google's securetoken certificate endpoint."""

    def __init__(self, certs: dict[str, str]) -> None:
        self.certs = certs
        self.hits = 0
        self.status = 200
        self.max_age = 3600

    async def handle(self, request: web.Request) -> web.Response:
        self.hits += 1
        if self.status != 200:
            return web.Response(status=self.status)
        return web.json_response(self.certs, headers={
            "Cache-Control": f"public, max-age={self.max_age}, must-revalidate, no-transform"})


class Env:
    def __init__(self, tmp: Path, client: TestClient, certs: CertServer, clock: list[float]):
        self.tmp, self.client, self.certs, self.clock = tmp, client, certs, clock

    def write_config(self, cfg) -> None:
        text = cfg if isinstance(cfg, str) else json.dumps(cfg)
        (self.tmp / "fountain_auth.json").write_text(text)

    async def check(self, token: str | None, method: str | None = "GET",
                    uri: str | None = "/api/fountain/visits?device=1", **headers):
        h = dict(headers)
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        if method is not None:
            h["X-Forwarded-Method"] = method
        if uri is not None:
            h["X-Forwarded-Uri"] = uri
        return await self.client.get(CHECK, headers=h)


@pytest.fixture
async def env(tmp_path, keys):
    certs = CertServer({KID: _cert_pem(keys["google"])})
    cert_app = web.Application()
    cert_app.router.add_get("/certs", certs.handle)
    cert_server = TestServer(cert_app)
    await cert_server.start_server()

    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope",
           "data_dir": str(tmp_path), "media_root": str(tmp_path / "media")}
    app = create_panel_app(DeviceRegistry(), None, EventHub(), cfg)
    clock = [1000.0]
    gate = auth.auth_of(app)
    gate.keys.url = str(cert_server.make_url("/certs"))
    gate.keys.clock = lambda: clock[0]
    client = TestClient(TestServer(app))
    await client.start_server()
    e = Env(tmp_path, client, certs, clock)
    e.write_config({"project_id": PROJECT, "allowed_emails": ["Owner@Example.com"]})
    try:
        yield e
    finally:
        await client.close()
        await cert_server.close()


async def test_a_valid_token_is_allowed_and_names_the_user(env, keys):
    r = await env.check(mint(keys["google"], email="OWNER@example.com"))
    assert r.status == 200
    assert r.headers["X-Fountain-User"] == EMAIL


@pytest.mark.parametrize("claims", [
    {"exp": int(time.time()) - 120},
    {"aud": "someone-else"},
    {"aud": [PROJECT]},
    {"iss": "https://securetoken.google.com/someone-else"},
    {"iss": "https://accounts.google.com"},
    {"iat": int(time.time()) + 600},
    {"sub": ""},
    {"email": "stranger@example.com"},
    {"email_verified": False},
    {"email_verified": "true"},
], ids=["expired", "aud", "aud-list", "iss", "iss-google", "iat-future", "no-sub",
        "email-not-allowed", "unverified", "verified-as-string"])
async def test_a_token_failing_a_claim_is_refused(env, keys, claims):
    r = await env.check(mint(keys["google"], **claims))
    assert r.status == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    assert "X-Fountain-User" not in r.headers


async def test_an_unknown_key_or_a_bad_signature_is_refused_and_never_echoed(
        env, keys, caplog):
    caplog.set_level(logging.DEBUG)
    for token in (mint(keys["google"], kid="kid-unknown"), mint(keys["other"])):
        r = await env.check(token)
        assert r.status == 401
        assert token not in await r.text()
        assert token.split(".")[2] not in caplog.text


@pytest.mark.parametrize("authorization", [
    None, "", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "Bearer abc", "Bearer a.b.c",
    "Bearer a.b", "Bearer !!!.@@@.###",
], ids=["absent", "empty", "no-token", "blank-token", "basic", "one-part", "garbage",
        "two-parts", "not-base64"])
async def test_a_missing_or_malformed_token_is_refused(env, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    r = await env.check(None, **headers)
    assert r.status == 401


async def test_a_token_with_another_algorithm_is_refused(env, keys):
    good = mint(keys["google"])
    head = _b64(json.dumps({"alg": "none", "kid": KID}).encode())
    assert (await env.check(f"{head}.{good.split('.')[1]}.")).status == 401
    assert (await env.check(mint(keys["google"], header={"alg": "HS256"}))).status == 401


@pytest.mark.parametrize(("method", "uri"), [
    ("GET", "/api/fountain/visits"),
    ("GET", "/api/fountain/visits?device=10006762&date=2026-10-14"),
    ("GET", "/api/fountain/stats?device=1&days=30"),
    ("GET", "/api/fountain/alerts"),
    ("POST", "/api/fountain/visits/abc123/label"),
    ("DELETE", "/api/fountain/visits/abc123/label?device=1"),
    ("GET", "/api/media/10006762/Playback/2026-10-14/clip%20one.mp4"),
    ("GET", "/api/media/thumb/10006762/Snapshots/a.jpg?w=320"),
    ("GET", "/api/pets"),
    ("GET", "/api/pets/3/faces/7/photo?v=2"),
])
async def test_the_allowed_routes_pass_with_query_strings(env, keys, method, uri):
    assert (await env.check(mint(keys["google"]), method, uri)).status == 200


@pytest.mark.parametrize(("method", "uri"), [
    ("GET", "/api/fountain/alert-settings"),
    ("POST", "/api/fountain/alert-settings"),
    ("POST", "/api/fountain/export"),
    ("GET", "/api/fountain/auth/check"),
    ("GET", "/api/fountain/visits/abc/label"),
    ("PUT", "/api/fountain/visits/abc/label"),
    ("POST", "/api/fountain/visits"),
    ("HEAD", "/api/fountain/visits"),
    ("get", "/api/fountain/visits"),
    ("POST", "/api/pets"),
    ("POST", "/api/pets/import"),
    ("GET", "/api/pets/3"),
    ("DELETE", "/api/pets/3/faces/7"),
    ("POST", "/api/pets/3/faces"),
    ("GET", "/api/settings"),
    ("POST", "/api/devices/1/command"),
    ("GET", "/api/media"),
    ("GET", "/api/media/"),
    ("GET", "/api/media/../settings"),
    ("GET", "/api/media/%2e%2e/%2E%2E/settings"),
    ("GET", "/api/fountain/visits/..%2F..%2Fsettings/label"),
    ("GET", "/api/pets/3/faces/7/photo/../../../../settings"),
    ("GET", "/api/media/a\\..\\b"),
    ("GET", "http://evil/api/pets"),
    ("GET", "//evil/api/pets"),
    ("GET", "api/pets"),
    (None, "/api/pets"),
    ("GET", None),
])
async def test_a_valid_user_on_any_other_route_gets_403(env, keys, method, uri):
    r = await env.check(mint(keys["google"]), method, uri)
    assert r.status == 403
    assert "X-Fountain-User" not in r.headers


async def test_keys_are_fetched_once_and_again_after_max_age(env, keys):
    token = mint(keys["google"])
    for _ in range(4):
        assert (await env.check(token)).status == 200
    assert env.certs.hits == 1
    env.clock[0] += 3599
    assert (await env.check(token)).status == 200
    assert env.certs.hits == 1
    env.clock[0] += 2
    assert (await env.check(token)).status == 200
    assert env.certs.hits == 2

    # Google unreachable after expiry: the keys held keep serving, and the
    # failed fetch is not retried on every request.
    env.certs.status = 500
    env.clock[0] += 3601
    assert (await env.check(token)).status == 200
    assert (await env.check(token)).status == 200
    assert env.certs.hits == 3


async def test_no_keys_and_google_unreachable_is_503(env, keys):
    env.certs.status = 500
    r = await env.check(mint(keys["google"]))
    assert r.status == 503
    env.clock[0] += auth.RETRY_AFTER_FAILURE_SEC + 1
    env.certs.status = 200
    assert (await env.check(mint(keys["google"]))).status == 200


@pytest.mark.parametrize("config", [
    None, "not json", [], {"allowed_emails": [EMAIL]},
    {"project_id": PROJECT}, {"project_id": PROJECT, "allowed_emails": []},
    {"project_id": PROJECT, "allowed_emails": EMAIL},
    {"project_id": "Bad Id", "allowed_emails": [EMAIL]},
], ids=["missing", "not-json", "not-object", "no-project", "no-emails", "empty-emails",
        "emails-not-list", "bad-project"])
async def test_a_missing_or_invalid_config_denies_everything(env, keys, caplog, config):
    path = env.tmp / "fountain_auth.json"
    path.unlink()
    if config is not None:
        env.write_config(config)
    token = mint(keys["google"])
    with caplog.at_level(logging.WARNING, logger=auth.__name__):
        for _ in range(3):
            assert (await env.check(token)).status == 503
    assert caplog.text.count("denying every request") == 1
    assert env.certs.hits == 0

    env.write_config({"project_id": PROJECT, "allowed_emails": [EMAIL]})
    r = await env.check(token)
    assert r.status == 200
    assert r.headers["X-Fountain-User"] == EMAIL


async def test_saving_alert_settings_leaves_the_allowlist_alone(env, keys):
    path = env.tmp / "fountain_auth.json"
    before = path.read_bytes()
    r = await env.client.post("/api/fountain/alert-settings", json={"no_drink_hours": 24})
    assert r.status == 200
    assert json.loads((env.tmp / "fountain_app.json").read_text()) == {"no_drink_hours": 24.0}
    assert path.read_bytes() == before
    assert (await env.check(mint(keys["google"]))).status == 200
