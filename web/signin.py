"""
The direct port's sign-in: Home Assistant's own login, administrators only (direct port login design,
docs/history/design/EMT-v5_direct_port_login_design.md, §3.2 and §3.5; checked on a real install, §6).

Ingress (the sidebar) is untouched: the Supervisor has already signed the user in, and IngressMiddleware
marks those requests `emt.ingress`. On the direct port, every request needs EMT's session cookie, apart
from the sign-in's own pages and static files. Without one, a page is sent to `/signin` and an API answers
401. With one, a write must also come from EMT's own page (`Origin`), because a `Lax` cookie still travels
on a POST from another port of the same host.

The sign-in is OAuth with Home Assistant (IndieAuth): `client_id` is EMT's origin as the browser reached
it, the redirect comes back to `/signin/done`, with a PKCE challenge. EMT exchanges the code itself, asks
`auth/current_user` who signed in, admits an administrator only, and revokes Home Assistant's tokens at
once: EMT needs to know who you are, not to act as you. Then it sets its own cookie, signed with a secret
kept in its own file beside the database (never in the database, which backups and support files carry).

`configure()` switches it on. Only `server.init()` calls it, which only main.py calls, so the suite and
the local preview (`.build/serve_local.py`, bound to 127.0.0.1) run without it unless a test asks.
"""
import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

from flask import Blueprint, jsonify, make_response, redirect, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

log = logging.getLogger(__name__)
bp = Blueprint("signin", __name__)

COOKIE = "emt_session"                # the stem: each install's cookie is `cookie_name()`
MAX_AGE = 30 * 24 * 3600              # a sign-in lasts 30 days in a browser
SECRET_FILE = "session_secret"        # beside blocks.db; not in a backup (blocks.db only) or support file
_STATE_TTL = 600                      # an unfinished sign-in is forgotten after 10 minutes
_STATE_MAX = 200

_cfg = {"required": False, "mode": "supervised", "secret": None}
_pending = {}                         # state -> {verifier, next, at}
_lock = threading.Lock()
_ha_cfg_cache = {"at": 0.0, "internal_url": None}


# ── configuration ─────────────────────────────────────────────────────────────

def load_secret(data_dir: str) -> bytes:
    """The session secret, made once and kept: a new one at every start (as `app.secret_key` is) would
    sign everyone out on each restart and update."""
    path = os.path.join(data_dir, SECRET_FILE)
    try:
        with open(path, "rb") as f:
            s = f.read().strip()
        if len(s) >= 32:
            return s
    except FileNotFoundError:
        pass
    s = secrets.token_hex(32).encode()
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(s)
    os.replace(path + ".tmp", path)
    return s


def configure(data_dir: str, mode: str = None, login: str = None, preview: bool = False) -> bool:
    """Switch the sign-in on for this process. Standalone may turn it off (`EMT_DIRECT_LOGIN=off`, the way
    out of a Home Assistant address that cannot be reached, §3.5); an add-on cannot, as its sidebar is
    always there. `preview`: `.build/serve_local.py`, bound to 127.0.0.1 with no Home Assistant to sign in
    to, is off unless EMT_PREVIEW_SIGNIN=1 asks for it. Returns whether it is on."""
    mode = mode if mode is not None else os.environ.get("EMT_MODE", "supervised")
    login = (login if login is not None else os.environ.get("EMT_DIRECT_LOGIN", "")).strip().lower()
    _cfg["mode"] = mode
    _cfg["required"] = not (mode == "standalone" and login == "off")
    if preview and os.environ.get("EMT_PREVIEW_SIGNIN") != "1":
        _cfg["required"] = False
        _cfg["secret"] = None
        log.info("signin: local preview, bound to this machine: no sign-in (EMT_PREVIEW_SIGNIN=1 for one)")
        return False
    _cfg["secret"] = load_secret(data_dir) if _cfg["required"] else None
    if _cfg["required"]:
        import instance as _inst
        _cfg["cookie"] = "%s_%s" % (COOKIE, hashlib.sha256(
            ("emt-session-cookie|" + _inst.get_instance_id(data_dir)).encode()).hexdigest()[:10])
    if _cfg["required"]:
        log.info("signin: the direct port asks for Home Assistant's sign-in (administrators only)")
    else:
        log.warning("signin: EMT_DIRECT_LOGIN=off — the direct port has NO login: anyone who can reach it "
                    "can use every tool")
    return _cfg["required"]


def required() -> bool:
    return _cfg["required"]


# ── the session cookie ────────────────────────────────────────────────────────

def _serializer():
    return URLSafeTimedSerializer(_cfg["secret"], salt="emt-direct-session")


def make_session(user_id: str) -> str:
    return _serializer().dumps({"u": user_id})


def read_session(value) -> dict | None:
    if not value or not _cfg["secret"]:
        return None
    try:
        return _serializer().loads(value, max_age=MAX_AGE)
    except BadSignature:                       # includes SignatureExpired
        return None


def cookie_name() -> str:
    """The session cookie's name: `emt_session_<10 hex>`, this install's own (20 Nov 2026, the user: signing in
    to staging on the same box signed prod out). A browser keeps a cookie by host — not by port, nor by the
    path a proxy puts EMT under — so two EMTs reached at one host shared `emt_session`: each sign-in replaced
    the other's cookie with one signed by its own secret, which the other cannot read. The suffix is a hash of
    the install's id (`instance.get_instance_id`, kept in its data folder), set by `configure`, so each install
    keeps its own whatever the address. A cookie of the old name is left alone: it may be another EMT's."""
    return _cfg.get("cookie") or COOKIE


def _set_cookie(resp, value, max_age=MAX_AGE):
    resp.set_cookie(cookie_name(), value, max_age=max_age, httponly=True, samesite="Lax", path="/",
                    secure=request.scheme == "https")
    return resp


# ── the rules ─────────────────────────────────────────────────────────────────

_EXEMPT = ("/signin", "/signin/start", "/signin/done", "/signin/check", "/favicon.ico")


def exempt(path: str) -> bool:
    """What the direct port serves without a session: the sign-in's own pages, and static files. Not
    `/signin/out`: signing out is a write, so the guard's Origin check applies to it."""
    return path in _EXEMPT or path.startswith("/static/")


def safe_next(value) -> str:
    """Where to go after signing in: a path on this server, never another site (`//evil`, `https://…`)."""
    v = (value or "").strip()
    if not v.startswith("/") or v.startswith("//") or "\\" in v or any(ord(c) < 32 for c in v):
        return "/"
    return "/" + v[1:]      # the same path, built on a constant "/" so CodeQL sees the redirect as local


def origin_ok(req) -> bool:
    """A write on the direct port must come from EMT's own pages: its Origin (else Referer) is this host."""
    if req.method in ("GET", "HEAD", "OPTIONS"):
        return True
    src = req.headers.get("Origin") or req.headers.get("Referer") or ""
    if not src or src == "null":
        return False
    try:
        return urlsplit(src).netloc == req.host
    except ValueError:
        return False


def guard():
    """Flask before_request. None lets the request through."""
    if not _cfg["required"] or request.environ.get("emt.ingress"):
        return None
    path = request.path or "/"
    if exempt(path):
        return None
    if read_session(request.cookies.get(cookie_name())) is None:
        if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
            return jsonify({"error": "Sign in with Home Assistant first.", "signin": "/signin"}), 401
        nxt = request.full_path if request.query_string else path
        return redirect("/signin?" + urlencode({"next": safe_next(nxt)}))
    if not origin_ok(request):
        log.warning("signin: refused a %s to %s from another origin", request.method, path)
        return jsonify({"error": "Refused: this request did not come from EMT's own page."}), 403
    return None


def signed_in() -> bool:
    """For the page: a direct-port session is open (the sidebar shows Sign out)."""
    return (_cfg["required"] and not request.environ.get("emt.ingress")
            and read_session(request.cookies.get(cookie_name())) is not None)


# ── where Home Assistant is ───────────────────────────────────────────────────

def _origin() -> str:
    return "%s://%s" % (request.scheme, request.host)


async def _core_config():
    import aiohttp
    tok = os.environ.get("SUPERVISOR_TOKEN", "")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
        async with s.get("http://supervisor/core/api/config", headers={"Authorization": "Bearer " + tok}) as r:
            return await r.json(content_type=None) if r.status == 200 else {}


def ha_public_url() -> str:
    """Where the browser goes for Home Assistant's sign-in (§3.2, §3.5)."""
    host = urlsplit(_origin()).hostname or "localhost"
    if ":" in host:
        host = "[%s]" % host
    if _cfg["mode"] == "standalone":
        pub = (os.environ.get("HA_PUBLIC_URL") or "").strip().rstrip("/")
        if pub:
            return pub
        ha = urlsplit(os.environ.get("HA_URL") or "http://localhost:8123")
        return "%s://%s:%s" % (ha.scheme or "http", host, ha.port or (443 if ha.scheme == "https" else 8123))
    now = time.time()
    if now - _ha_cfg_cache["at"] > 600:
        _ha_cfg_cache["at"] = now
        try:
            _ha_cfg_cache["internal_url"] = (asyncio.run(_core_config()).get("internal_url") or "").rstrip("/")
        except Exception as e:
            log.info("signin: Home Assistant's internal URL not read (%s); using this host", type(e).__name__)
            _ha_cfg_cache["internal_url"] = None
    return _ha_cfg_cache["internal_url"] or "http://%s:8123" % host


def ha_addresses() -> list:
    """Every address the sign-in page may send the browser to, best first (20 Nov 2026, the user's staging:
    Home Assistant's local address said `https://…:8123` while Home Assistant served plain HTTP there, so the
    browser could not connect). The address `ha_public_url` finds, then plain HTTP and HTTPS at this host's
    port 8123, once each. The page tries them from the browser and takes the first that answers; the server
    sends the browser only to one of these, by its place in the list — never to an address a request names."""
    host = urlsplit(_origin()).hostname or "localhost"
    if ":" in host:
        host = "[%s]" % host
    out = []
    for a in (ha_public_url(), "http://%s:8123" % host, "https://%s:8123" % host):
        if a and a not in out:
            out.append(a)
    return out


def ha_internal_url() -> str:
    """Where EMT itself reaches Home Assistant to finish the sign-in (§6: homeassistant:8123 on an add-on)."""
    if _cfg["mode"] == "standalone":
        return (os.environ.get("HA_URL") or "").rstrip("/")
    return "http://homeassistant:8123"


# ── finishing the sign-in with Home Assistant ─────────────────────────────────

# What the page says for each refusal. The page shows only these fixed words, never an exception's own
# text (CodeQL: information exposure through an exception); the details go to the log.
REFUSED = {
    "forbidden": "Home Assistant refused this sign-in: the user may not log in (inactive, for example).",
    "not_accepted": "Home Assistant did not accept the sign-in. Try again.",
    "token": "Home Assistant did not accept its own token. Try again.",
    "who": "Home Assistant did not say who signed in. Try again.",
}


class SigninRefused(Exception):
    """Why a sign-in ends without a session: a key of REFUSED."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def _finish(code: str, verifier: str, client_id: str) -> dict:
    """Code → tokens → auth/current_user → revoke. Returns the user ({id, is_admin}); raises SigninRefused."""
    import aiohttp
    base = ha_internal_url()
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(base + "/auth/token", data={"grant_type": "authorization_code", "code": code,
                                                      "client_id": client_id, "code_verifier": verifier}) as r:
            body = await r.text()
            if r.status == 403:
                raise SigninRefused("forbidden")
            if r.status != 200:
                log.warning("signin: Home Assistant's /auth/token answered %s", r.status)
                raise SigninRefused("not_accepted")
            tok = json.loads(body)
        user = {}
        try:
            ws_url = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/api/websocket"
            async with s.ws_connect(ws_url) as ws:
                await ws.receive_json()                                   # auth_required
                await ws.send_json({"type": "auth", "access_token": tok["access_token"]})
                if (await ws.receive_json()).get("type") != "auth_ok":
                    raise SigninRefused("token")
                await ws.send_json({"id": 1, "type": "auth/current_user"})
                user = (await ws.receive_json()).get("result") or {}
        finally:
            try:                                                          # EMT keeps none of Home Assistant's tokens
                async with s.post(base + "/auth/revoke", data={"token": tok.get("refresh_token", "")}) as r:
                    if r.status != 200:
                        log.warning("signin: revoking Home Assistant's token answered %s", r.status)
            except Exception as e:
                log.warning("signin: revoking Home Assistant's token failed: %s", type(e).__name__)
    if not user.get("id"):
        raise SigninRefused("who")
    return {"id": user["id"], "is_admin": bool(user.get("is_admin"))}


finish = _finish                      # the tests replace this


# ── the pages ─────────────────────────────────────────────────────────────────

_CSS = """<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#1d2129;--muted:#5b6170;--accent:#3b82f6;--border:#d9dce3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1117;--card:#1a1d27;--text:#e8eaf0;--muted:#8b90a0;--border:#2a2d3a}}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,sans-serif}
main{max-width:520px;margin:12vh auto;padding:28px 24px;background:var(--card);border:1px solid var(--border);
border-radius:12px}h1{font-size:20px;margin:0 0 12px}p{margin:10px 0}.muted{color:var(--muted);font-size:13px}
.btn{display:inline-block;margin-top:8px;padding:9px 16px;border-radius:8px;background:var(--accent);color:#fff;
text-decoration:none;border:0;font:inherit;cursor:pointer}code{font-size:13px}
#unreach{display:none;color:#c0392b}@media (max-width:560px){main{margin:16px}}
</style>"""


def _page(title, body, status=200):
    doc = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'><title>%s — EMT</title>%s</head>"
           "<body><main><h1>%s</h1>%s</main></body></html>") % (html.escape(title), _CSS, html.escape(title), body)
    resp = make_response(doc, status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _js(v) -> str:
    """A value as a JavaScript literal inside <script>: JSON, with `<` escaped so nothing in it (a Host
    header, say) can close the element."""
    return json.dumps(v).replace("<", "\\u003c")


@bp.route("/signin")
def signin_page():
    nxt = safe_next(request.args.get("next"))
    if not _cfg["required"]:
        return redirect(nxt)
    addrs = ha_addresses()
    ha = addrs[0]
    standalone = _cfg["mode"] == "standalone"
    fix = ("Set <code>HA_PUBLIC_URL</code> to the address you open Home Assistant at, and restart EMT."
           if standalone else
           "Open EMT from Home Assistant's sidebar instead, or set Home Assistant's local address "
           "(Settings → System → Network).")
    body = ("<p>EMT on this port asks you to sign in with your Home Assistant account. "
            "Only Home Assistant administrators can use it.</p>"
            "<a class=btn id=go href='/signin/start?%s'>Sign in with Home Assistant</a>"
            "<p class=muted>Home Assistant at <code id=at>%s</code></p>"
            "<p id=unreach>Home Assistant doesn't answer at that address from this browser. %s</p>"
            # each address tried from this browser at once; the first in the list that answers is used
            "<script>(function(){var a=%s,q=%s,ok=a.map(function(){return null});"
            "function pick(){for(var i=0;i<a.length;i++){if(ok[i]===null)return;if(ok[i]){"
            "document.getElementById('at').textContent=a[i];"
            "document.getElementById('go').href='/signin/start?'+q+'&ha='+i;return}}"
            "document.getElementById('unreach').style.display='block'}"
            "a.forEach(function(u,i){var c=new AbortController();setTimeout(function(){c.abort()},6000);"
            "fetch(u+'/manifest.json',{mode:'no-cors',signal:c.signal}).then(function(){ok[i]=true;pick()},"
            "function(){ok[i]=false;pick()})})})();</script>") % (
        urlencode({"next": nxt}), html.escape(ha), fix, _js(addrs), _js(urlencode({"next": nxt})))
    return _page("Sign in", body)


@bp.route("/signin/start")
def signin_start():
    if not _cfg["required"]:
        return redirect("/")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        for k in [k for k, v in _pending.items() if now - v["at"] > _STATE_TTL]:
            _pending.pop(k, None)
        while len(_pending) >= _STATE_MAX:
            _pending.pop(min(_pending, key=lambda k: _pending[k]["at"]), None)
        _pending[state] = {"verifier": verifier, "next": safe_next(request.args.get("next")), "at": now}
    origin = _origin()
    q = {"response_type": "code", "client_id": origin + "/", "redirect_uri": origin + "/signin/done",
         "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    addrs = ha_addresses()
    i = request.args.get("ha", "0")
    i = int(i) if i.isdigit() and int(i) < len(addrs) else 0       # a place in EMT's own list, nothing else
    return redirect(addrs[i] + "/auth/authorize?" + urlencode(q))


@bp.route("/signin/done")
def signin_done():
    if not _cfg["required"]:
        return redirect("/")
    with _lock:
        p = _pending.pop(request.args.get("state", ""), None)
    again = "<a class=btn href='/signin'>Sign in again</a>"
    if not p or time.time() - p["at"] > _STATE_TTL:
        return _page("Sign in", "<p>That sign-in had expired, or was already used.</p>" + again, 400)
    code = request.args.get("code")
    if request.args.get("error") or not code:
        return _page("Sign in", "<p>Home Assistant didn't complete the sign-in.</p>" + again, 400)
    try:
        user = asyncio.run(finish(code, p["verifier"], _origin() + "/"))
    except SigninRefused as e:
        say = REFUSED.get(e.reason, REFUSED["not_accepted"])
        return _page("Sign in", "<p>%s</p>%s" % (html.escape(say), again), 403)
    except Exception as e:
        log.warning("signin: finishing the sign-in failed: %s", type(e).__name__)
        return _page("Sign in", "<p>EMT couldn't reach Home Assistant to finish the sign-in. "
                     "Try again in a moment.</p>" + again, 502)
    if not user["is_admin"]:
        log.info("signin: a Home Assistant user who is not an administrator was refused")
        return _page("Sign in", "<p>EMT is for Home Assistant administrators. Ask one to use it, or open EMT "
                     "from Home Assistant's sidebar if your account has it.</p>", 403)
    log.info("signin: an administrator signed in on the direct port")
    resp = redirect("/signin/check?" + urlencode({"next": p["next"]}))
    return _set_cookie(resp, make_session(user["id"]))


@bp.route("/signin/check")
def signin_check():
    """After the cookie is set: did the browser keep it? In a frame on another address it does not (§6), and
    another sign-in would loop; this page says why instead."""
    nxt = safe_next(request.args.get("next"))
    if not _cfg["required"] or read_session(request.cookies.get(cookie_name())) is not None:
        return redirect(nxt)
    body = ("<p>You signed in, but this browser didn't keep EMT's sign-in. That happens when EMT is shown "
            "inside a Home Assistant page opened at a different address from EMT's: one by name "
            "(<code>homeassistant.local</code>), the other by IP address.</p>"
            "<p>Open Home Assistant at the same address as EMT, or use EMT in its own tab.</p>"
            "<a class=btn target=_blank rel=noopener href='%s'>Open EMT in a new tab</a>") % html.escape(
                _origin() + nxt, quote=True)
    return _page("Signed in, but not kept", body)


@bp.route("/signin/out", methods=["POST"])
def signout():
    """Sign out: the cookie goes. A POST, so the guard's Origin check applies first."""
    resp = _page("Signed out", "<p>You've signed out of EMT on this port.</p>"
                 "<a class=btn href='/signin'>Sign in again</a>")
    return _set_cookie(resp, "", max_age=0)
