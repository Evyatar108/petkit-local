"""The phone app's sign-in check: `GET /api/fountain/auth/check`.

Caddy's `forward_auth` calls this before proxying any request for the phone
app's host, passing the phone's `Authorization: Bearer <Firebase ID token>`
and the original request as `X-Forwarded-Method` and `X-Forwarded-Uri`. It
answers 200 with `X-Fountain-User: <email>` when the token is a valid Firebase
ID token for an allowlisted, verified email AND the original request is one
the phone may make (`ALLOWED_ROUTES`; Caddy enforces the same list, this is
the second line). Otherwise 401 (no or bad token, email not allowed), 403 (a
valid user asking for a route outside the list) or 503 (sign-in not
configured, or Google's keys unavailable and none cached).

The token is checked the way Firebase documents for third-party verifiers:
RS256 against Google's published `securetoken` certificates (cached per their
`Cache-Control: max-age`), `aud` the project id, `iss`
`https://securetoken.google.com/<project id>`, `exp`/`iat` with a minute of
clock skew, a non-empty `sub`.

The project id and allowed emails live in `{data_dir}/fountain_auth.json`
(`{"project_id": "...", "allowed_emails": ["..."]}`), never in
`fountain_app.json`: the alert settings writer replaces that whole file. The
file is re-read when it changes; while it is missing or invalid, every request
is denied and the reason is logged once. The token itself is never logged or
echoed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp import web
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger(__name__)

CERTS_URL = ("https://www.googleapis.com/robot/v1/metadata/x509/"
             "securetoken@system.gserviceaccount.com")
ISSUER_PREFIX = "https://securetoken.google.com/"
CONFIG_FILE = "fountain_auth.json"
USER_HEADER = "X-Fountain-User"
CLOCK_SKEW_SEC = 60
#: Key lifetime when Google's answer carries no `max-age`, and the most any
#: answer is trusted for.
DEFAULT_KEYS_MAX_AGE_SEC = 300
MAX_KEYS_MAX_AGE_SEC = 86400
#: After a failed fetch, requests wait this long before the next attempt.
RETRY_AFTER_FAILURE_SEC = 60
#: While Google is unreachable, expired keys keep serving for this long.
STALE_KEYS_GRACE_SEC = 86400
FETCH_TIMEOUT_SEC = 10
MAX_TOKEN_LEN = 8192
#: The `app[...]` key holding this app's `AuthGate`, set by `register`.
FOUNTAIN_AUTH = "fountain_auth"

_ID = r"[^/]+"
#: The method and path pairs the phone may reach (query strings allowed).
ALLOWED_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(path)) for method, path in (
        ("GET", r"/api/fountain/visits"),
        ("GET", r"/api/fountain/stats"),
        ("GET", r"/api/fountain/alerts"),
        ("POST", rf"/api/fountain/visits/{_ID}/label"),
        ("DELETE", rf"/api/fountain/visits/{_ID}/label"),
        ("GET", r"/api/media/.+"),
        ("GET", r"/api/pets"),
        ("GET", rf"/api/pets/{_ID}/faces/{_ID}/photo"),
    )
)


class Denied(Exception):
    """A refused check: the HTTP status to answer and a short reason."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


# --- routes -------------------------------------------------------------------


def route_allowed(method: str | None, uri: str | None) -> bool:
    """True if `method uri` (origin-form, query allowed) is on `ALLOWED_ROUTES`.

    The path is matched percent-decoded, and refused outright if any segment
    is `.` or `..` or it holds a backslash or NUL, so an encoded or dotted
    path cannot reach a route the list does not name.
    """
    if not method or not uri or not uri.startswith("/"):
        return False
    parts = urlsplit(uri)
    if parts.scheme or parts.netloc:
        return False
    path = unquote(parts.path)
    if "\\" in path or "\x00" in path or any(s in (".", "..") for s in path.split("/")):
        return False
    return any(m == method and p.fullmatch(path) for m, p in ALLOWED_ROUTES)


# --- config -------------------------------------------------------------------


@dataclass(frozen=True)
class AuthConfig:
    project_id: str
    allowed_emails: frozenset[str]


def parse_config(raw: Any) -> AuthConfig:
    """`fountain_auth.json`'s contents as an `AuthConfig`.

    Raises:
        ValueError: the reason the contents are unusable.
    """
    if not isinstance(raw, dict):
        raise ValueError("it is not a JSON object")
    project_id = raw.get("project_id")
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", project_id):
        raise ValueError("project_id must be a Firebase project id")
    emails = raw.get("allowed_emails")
    if not isinstance(emails, list) or not all(isinstance(e, str) for e in emails):
        raise ValueError("allowed_emails must be a list of strings")
    allowed = frozenset(e.strip().lower() for e in emails if e.strip())
    if not allowed:
        raise ValueError("allowed_emails is empty")
    return AuthConfig(project_id, allowed)


class ConfigSource:
    """`{data_dir}/fountain_auth.json`, re-read whenever the file changes."""

    def __init__(self) -> None:
        self._stamp: tuple[int, int, int] | None = None
        self._config: AuthConfig | None = None
        self._reason: str | None = None

    def load(self, data_dir: str | None) -> AuthConfig | None:
        """The current config, or None (logged once per reason) if unusable."""
        path = os.path.join(data_dir, CONFIG_FILE) if data_dir else None
        try:
            st = os.stat(path) if path else None
        except OSError:
            st = None
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino) if st else None
        if stamp is not None and stamp == self._stamp:
            return self._config
        self._stamp = stamp
        reason = None
        if path is None:
            reason = "no data directory configured"
        elif st is None:
            reason = f"{path} does not exist"
        else:
            try:
                with open(path, "rb") as f:
                    self._config = parse_config(json.loads(f.read().decode("utf-8")))
            except (OSError, ValueError) as e:
                reason = f"{path} is unusable: {e}"
        if reason is not None:
            self._config = None
            if reason != self._reason:
                log.warning("fountain auth: denying every request: %s", reason)
        elif self._config is not None:
            log.info("fountain auth: config loaded (%d allowed emails)",
                     len(self._config.allowed_emails))
        self._reason = reason
        return self._config


# --- Google's keys ------------------------------------------------------------


def parse_max_age(cache_control: str | None) -> int:
    """Seconds from a `Cache-Control` header's `max-age`, clamped; a default without one."""
    m = re.search(r"(?:^|[,\s])max-age\s*=\s*\"?(\d+)", cache_control or "", re.IGNORECASE)
    if not m:
        return DEFAULT_KEYS_MAX_AGE_SEC
    return min(int(m.group(1)), MAX_KEYS_MAX_AGE_SEC)


def parse_certs(body: Any) -> dict[str, rsa.RSAPublicKey]:
    """`{kid: PEM certificate}` as `{kid: RSA public key}`.

    Raises:
        ValueError: the body is not that shape, or holds no RSA key.
    """
    if not isinstance(body, dict):
        raise ValueError("certificates are not a JSON object")
    keys: dict[str, rsa.RSAPublicKey] = {}
    for kid, pem in body.items():
        if not isinstance(pem, str):
            continue
        key = x509.load_pem_x509_certificate(pem.encode("ascii")).public_key()
        if isinstance(key, rsa.RSAPublicKey):
            keys[kid] = key
    if not keys:
        raise ValueError("no RSA certificates")
    return keys


class GoogleKeys:
    """Google's token-signing keys, fetched on demand and cached per `max-age`.

    One fetch at a time. A failed fetch keeps any keys already held for up to
    `STALE_KEYS_GRACE_SEC` past their expiry, and is not retried for
    `RETRY_AFTER_FAILURE_SEC`.
    """

    def __init__(self, url: str = CERTS_URL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.url = url
        self.clock = clock
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._expires = 0.0
        self._retry_at = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self) -> tuple[dict[str, rsa.RSAPublicKey], int]:
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self.url) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                body = await resp.json(content_type=None)
                max_age = parse_max_age(resp.headers.get("Cache-Control"))
        return parse_certs(body), max_age

    async def get(self) -> dict[str, rsa.RSAPublicKey] | None:
        """The current keys, or None if there are none to verify with."""
        if self._keys and self.clock() < self._expires:
            return self._keys
        async with self._lock:
            now = self.clock()
            if self._keys and now < self._expires:
                return self._keys
            if now >= self._retry_at:
                try:
                    keys, max_age = await self._fetch()
                except Exception as e:
                    self._retry_at = now + RETRY_AFTER_FAILURE_SEC
                    log.warning("fountain auth: could not fetch Google's token keys: %s: %s",
                                type(e).__name__, e)
                else:
                    self._keys, self._expires, self._retry_at = keys, now + max_age, 0.0
                    return keys
            if self._keys and now < self._expires + STALE_KEYS_GRACE_SEC:
                return self._keys
            return None


# --- the token ----------------------------------------------------------------


def _b64decode(part: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", part):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def bearer_token(header: str | None) -> str:
    """The token in an `Authorization: Bearer ...` header.

    Raises:
        Denied: 401, the header is absent or not a bearer token.
    """
    if not header:
        raise Denied(401, "missing token")
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise Denied(401, "malformed token")
    return token


def split_token(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """`(header, claims, signed bytes, signature)` of a compact JWS.

    Raises:
        Denied: 401, the token is not a well-formed RS256 JWT with a `kid`.
    """
    parts = token.split(".")
    if len(token) > MAX_TOKEN_LEN or len(parts) != 3:
        raise Denied(401, "malformed token")
    try:
        header = json.loads(_b64decode(parts[0]))
        claims = json.loads(_b64decode(parts[1]))
        signature = _b64decode(parts[2])
    except (ValueError, RecursionError):
        raise Denied(401, "malformed token")
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise Denied(401, "malformed token")
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise Denied(401, "malformed token")
    return header, claims, f"{parts[0]}.{parts[1]}".encode("ascii"), signature


def _number(claims: dict[str, Any], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Denied(401, f"token has no {name}")
    return float(value)


def check_claims(claims: dict[str, Any], config: AuthConfig, now: float) -> str:
    """The allowlisted email `claims` belong to, lowercased.

    Raises:
        Denied: 401, the claims fail a Firebase ID token rule or the allowlist.
    """
    if claims.get("aud") != config.project_id:
        raise Denied(401, "wrong audience")
    if claims.get("iss") != ISSUER_PREFIX + config.project_id:
        raise Denied(401, "wrong issuer")
    if _number(claims, "exp") <= now - CLOCK_SKEW_SEC:
        raise Denied(401, "token expired")
    if _number(claims, "iat") > now + CLOCK_SKEW_SEC:
        raise Denied(401, "token issued in the future")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise Denied(401, "token has no subject")
    if claims.get("email_verified") is not True:
        raise Denied(401, "email not verified")
    email = claims.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    if not email or email not in config.allowed_emails:
        raise Denied(401, "email not allowed")
    return email


class AuthGate:
    """The check's state on the panel app: the config file and Google's keys."""

    def __init__(self) -> None:
        self.config = ConfigSource()
        self.keys = GoogleKeys()

    async def check(self, data_dir: str | None, authorization: str | None,
                    method: str | None, uri: str | None) -> str:
        """The allowed user's email for one forwarded request.

        Raises:
            Denied: why the request is refused, and with which status.
        """
        config = self.config.load(data_dir)
        if config is None:
            raise Denied(503, "sign-in is not configured")
        header, claims, signed, signature = split_token(bearer_token(authorization))
        keys = await self.keys.get()
        if keys is None:
            raise Denied(503, "token keys unavailable")
        key = keys.get(header["kid"])
        if key is None:
            raise Denied(401, "unknown signing key")
        try:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise Denied(401, "bad signature")
        email = check_claims(claims, config, time.time())
        if not route_allowed(method, uri):
            raise Denied(403, "route not allowed")
        return email


def auth_of(app: web.Application) -> AuthGate:
    """This app's `AuthGate`."""
    return app[FOUNTAIN_AUTH]


async def api_fountain_auth_check(request: web.Request) -> web.Response:
    """`GET /api/fountain/auth/check`, for Caddy's `forward_auth`. See the module doc."""
    try:
        email = await auth_of(request.app).check(
            request.app["cfg"].get("data_dir"), request.headers.get("Authorization"),
            request.headers.get("X-Forwarded-Method"), request.headers.get("X-Forwarded-Uri"))
    except Denied as d:
        log.info("fountain auth: refused %s (%d)", d.reason, d.status)
        headers = {"WWW-Authenticate": "Bearer"} if d.status == 401 else None
        return web.json_response({"error": d.reason}, status=d.status, headers=headers)
    return web.json_response({"ok": True}, headers={USER_HEADER: email})
