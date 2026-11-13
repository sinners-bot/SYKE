"""Security core for the owner web dashboard: signed sessions, CSRF tokens, the owner gate.

Kept free of aiohttp so it can be reasoned about and unit-tested on its own. Everything here is
stdlib only. Sessions are signed (HMAC-SHA256) but not encrypted; they hold a user id and an
expiry, nothing secret. The OAuth `state` and the CSRF token are signed the same way.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

SESSION_COOKIE = "syke_session"
SESSION_TTL = 7 * 24 * 3600
STATE_TTL = 600
OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
OAUTH_USER = "https://discord.com/api/users/@me"
SCOPE = "identify"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(secret: str, payload: bytes) -> str:
    return _b64(hmac.new(secret.encode(), payload, hashlib.sha256).digest())


def sign(secret: str, data: dict) -> str:
    """A tamper-proof token carrying `data`. Format: base64(json).signature."""
    body = _b64(json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(secret, body.encode())}"


def unsign(secret: str, token: str, *, max_age: int | None = None) -> dict | None:
    """The data in `token`, or None if it's missing, altered, or past `max_age`."""
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    expected = _sign(secret, body.encode())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if max_age is not None:
        issued = data.get("t")
        if not isinstance(issued, (int, float)) or time.time() - issued > max_age:
            return None
    return data


def make_session(secret: str, user_id: int, name: str) -> str:
    return sign(secret, {"u": int(user_id), "n": name[:80], "t": int(time.time())})


def session_user(secret: str, token: str, owner_ids) -> tuple[int, str] | None:
    """`(user_id, name)` for a valid session belonging to an owner, else None."""
    data = unsign(secret, token, max_age=SESSION_TTL)
    if data is None:
        return None
    uid = data.get("u")
    if not isinstance(uid, int) or uid not in owner_ids:
        return None
    return uid, str(data.get("n", "owner"))


def make_state(secret: str) -> str:
    return sign(secret, {"r": secrets.token_urlsafe(12), "t": int(time.time())})


def valid_state(secret: str, token: str) -> bool:
    return unsign(secret, token, max_age=STATE_TTL) is not None


def csrf_token(secret: str, user_id: int) -> str:
    """A CSRF token bound to the logged-in user. Checked on every state-changing POST."""
    return sign(secret, {"c": int(user_id)})


def valid_csrf(secret: str, token: str, user_id: int) -> bool:
    data = unsign(secret, token)
    return bool(data and data.get("c") == int(user_id))


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "prompt": "none",
    })
    return f"{OAUTH_AUTHORIZE}?{query}"


def redirect_uri(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/callback"


def token_request(client_id: str, client_secret: str, code: str, redirect: str) -> dict:
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect,
    }
