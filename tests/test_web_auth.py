import time

from syke import web_auth as auth


SECRET = "test-secret-value"


def test_sign_roundtrip_and_tamper():
    now = int(time.time())
    token = auth.sign(SECRET, {"u": 5, "t": now})
    assert auth.unsign(SECRET, token) == {"u": 5, "t": now}
    # Wrong secret fails.
    assert auth.unsign("other", token) is None
    # Tampered body fails.
    body, _, sig = token.partition(".")
    assert auth.unsign(SECRET, "x" + body + "." + sig) is None
    assert auth.unsign(SECRET, "notatoken") is None
    assert auth.unsign(SECRET, "") is None


def test_session_owner_gate():
    token = auth.make_session(SECRET, 42, "Owner")
    assert auth.session_user(SECRET, token, {42}) == (42, "Owner")
    # Not an owner.
    assert auth.session_user(SECRET, token, {1, 2}) is None
    # Garbage.
    assert auth.session_user(SECRET, "junk", {42}) is None


def test_session_expiry():
    old = auth.sign(SECRET, {"u": 42, "n": "o", "t": int(time.time()) - auth.SESSION_TTL - 10})
    assert auth.session_user(SECRET, old, {42}) is None


def test_state_roundtrip_and_expiry():
    assert auth.valid_state(SECRET, auth.make_state(SECRET))
    assert not auth.valid_state(SECRET, "forged.sig")
    old = auth.sign(SECRET, {"r": "x", "t": int(time.time()) - auth.STATE_TTL - 5})
    assert not auth.valid_state(SECRET, old)


def test_csrf_is_bound_to_user():
    token = auth.csrf_token(SECRET, 7)
    assert auth.valid_csrf(SECRET, token, 7)
    assert not auth.valid_csrf(SECRET, token, 8)
    assert not auth.valid_csrf(SECRET, "nope", 7)


def test_authorize_url():
    url = auth.authorize_url("12345", "https://x.app/callback", "statevalue")
    assert url.startswith(auth.OAUTH_AUTHORIZE)
    assert "client_id=12345" in url and "scope=identify" in url
    assert "redirect_uri=https%3A%2F%2Fx.app%2Fcallback" in url
    assert "state=statevalue" in url


def test_redirect_uri():
    assert auth.redirect_uri("https://x.app/") == "https://x.app/callback"
    assert auth.redirect_uri("https://x.app") == "https://x.app/callback"
