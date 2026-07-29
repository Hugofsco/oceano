"""Auth routes: who-am-i, login/logout (with throttling), the account
(username/password) editor, and optional TOTP two-factor auth."""
import hmac
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response

from oceano.web.state import (
    INITIAL_PW_FILE,
    SESSION_COOKIE,
    _current_user,
    _hash_pw,
    _is_default_pw,
    _set_session_cookie,
    _totp_secret,
    _totp_uri,
    _totp_verify,
    load,
    save,
)

router = APIRouter()


# ---------------- auth ----------------
@router.get("/api/me")
def whoami(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "not authenticated")
    return {"user": user, "must_change": _is_default_pw(load().get("auth", {}))}


# Throttle login (password AND 2FA-code) attempts per client IP — a brute force shouldn't be free,
# especially over a tunnel. Sliding window, in-memory; cleared on a successful login.
_LOGIN_FAILS = {}
_LOGIN_MAX, _LOGIN_WINDOW = 8, 300                      # >8 failures in 5 min → cool off


def _login_blocked(ip):
    now = time.monotonic()
    fails = [t for t in _LOGIN_FAILS.get(ip, ()) if now - t < _LOGIN_WINDOW]
    if fails:
        _LOGIN_FAILS[ip] = fails
    else:
        _LOGIN_FAILS.pop(ip, None)
    return len(fails) >= _LOGIN_MAX


def _login_fail(ip):
    _LOGIN_FAILS.setdefault(ip, []).append(time.monotonic())


@router.post("/api/login")
async def login(request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    if _login_blocked(ip):
        raise HTTPException(429, "too many attempts — wait a few minutes and try again")
    body = await request.json()
    data = load()
    auth = data.get("auth", {})
    user = (body.get("user") or "").strip()
    pw = body.get("password") or ""
    ok = (user == auth.get("user")
          and hmac.compare_digest(_hash_pw(pw, auth.get("salt", "")), auth.get("pwhash", "")))
    if not ok:
        _login_fail(ip)
        raise HTTPException(401, "invalid username or password")
    if auth.get("totp_enabled"):                        # second factor required
        code = (body.get("code") or "").strip()
        if not code:
            return {"ok": False, "need_code": True}     # password was right — UI now asks for the code
        step = _totp_verify(auth.get("totp_secret", ""), code)
        last = auth.get("totp_last_step")
        if step is None or (last is not None and step <= last):
            _login_fail(ip)
            raise HTTPException(401, "invalid authentication code")
        auth["totp_last_step"] = step                   # replay guard — this step can't be reused
        data["auth"] = auth
        save(data)
    _LOGIN_FAILS.pop(ip, None)                          # success → clear the counter
    _set_session_cookie(response, user, auth["secret"])
    return {"ok": True, "user": user, "must_change": _is_default_pw(auth)}


@router.post("/api/logout")
def logout(response: Response):
    # Rotate the cookie-signing secret so logging out actually REVOKES the session server-side,
    # not just on this browser — a cookie copied off the wire/disk dies here too. Single-user, so
    # this is "log out everywhere": any other open tab/device is signed out on its next request.
    data = load()
    if data.get("auth", {}).get("secret"):
        data["auth"]["secret"] = secrets.token_hex(32)
        save(data)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/api/account")
async def change_account(request: Request, response: Response):
    """Change the username/password. Gated by the middleware; requires the current
    password too, so a hijacked open tab can't silently rotate credentials."""
    body = await request.json()
    data = load()
    auth = data["auth"]
    if not hmac.compare_digest(_hash_pw(body.get("current_password") or "", auth["salt"]), auth["pwhash"]):
        raise HTTPException(403, "current password is incorrect")
    new_user = (body.get("user") or auth["user"]).strip() or auth["user"]
    new_pw = body.get("new_password") or ""
    if new_pw.strip().lower() == "admin":          # don't let the forced change loop back to the default
        raise HTTPException(400, "choose a password other than the default 'admin'")
    if new_pw:
        auth["salt"] = secrets.token_hex(16)
        auth["pwhash"] = _hash_pw(new_pw, auth["salt"])
        # Rotate the cookie-signing secret on a password change so EVERY other outstanding session
        # cookie is invalidated — the instinctive "I've been compromised, change my password" must
        # actually evict a thief's stolen cookie, which HMAC'ing with the old secret no longer will.
        auth["secret"] = secrets.token_hex(32)
        # The first-boot password has been replaced: clear the forced-change flag and drop the
        # 0600 copy of the seeded password so it can't linger on disk after it stops being valid.
        auth.pop("must_change", None)
        try:
            INITIAL_PW_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    auth["user"] = new_user
    data["auth"] = auth
    save(data)
    _set_session_cookie(response, new_user, auth["secret"])   # re-issue with the (possibly new) secret
    return {"ok": True, "user": new_user}


# ---------------- two-factor auth (optional TOTP) ----------------
@router.get("/api/2fa/status")
def twofa_status():
    auth = load().get("auth", {})
    return {"enabled": bool(auth.get("totp_enabled")), "pending": bool(auth.get("totp_pending"))}


@router.post("/api/2fa/setup")
def twofa_setup():
    """Mint a pending TOTP secret and return the otpauth URI + a QR (SVG) to scan. Not active until
    confirmed with a valid code via /api/2fa/enable, so a mis-scan can't lock the user out."""
    data = load()
    auth = data["auth"]
    secret = _totp_secret()
    auth["totp_pending"] = secret
    data["auth"] = auth
    save(data)
    uri = _totp_uri(secret, auth.get("user", "user"))
    try:
        import segno
        svg = segno.make(uri, error="m").svg_inline(scale=5)
    except Exception:
        svg = ""                                        # UI falls back to showing the secret/URI
    return {"ok": True, "uri": uri, "secret": secret, "svg": svg}


@router.post("/api/2fa/enable")
async def twofa_enable(req: Request):
    """Turn 2FA on: confirm the pending secret with a current code AND the account password — so a
    hijacked session (which has the cookie but not the password) can't silently enroll its own
    authenticator. Matches the password gate on change_account / 2fa disable."""
    data = load()
    auth = data["auth"]
    pending = auth.get("totp_pending")
    if not pending:
        raise HTTPException(400, "no pending 2FA setup — start with Set up")
    body = await req.json()
    if not hmac.compare_digest(_hash_pw(body.get("current_password") or "", auth.get("salt", "")), auth.get("pwhash", "")):
        raise HTTPException(403, "current password is incorrect")
    code = (body.get("code") or "").strip()
    step = _totp_verify(pending, code)
    if step is None:
        raise HTTPException(400, "that code didn't match — check your authenticator and try again")
    auth["totp_secret"] = pending
    auth["totp_enabled"] = True
    auth["totp_last_step"] = step                       # the confirming code can't be replayed at login
    auth.pop("totp_pending", None)
    data["auth"] = auth
    save(data)
    return {"ok": True, "enabled": True}


@router.post("/api/2fa/disable")
async def twofa_disable(req: Request):
    """Turn 2FA off. Requires the current password (a deliberate action), like change_account."""
    data = load()
    auth = data["auth"]
    pw = (await req.json()).get("current_password") or ""
    if not hmac.compare_digest(_hash_pw(pw, auth.get("salt", "")), auth.get("pwhash", "")):
        raise HTTPException(403, "current password is incorrect")
    for k in ("totp_enabled", "totp_secret", "totp_pending", "totp_last_step"):
        auth.pop(k, None)
    data["auth"] = auth
    save(data)
    return {"ok": True, "enabled": False}
