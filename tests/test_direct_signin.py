"""
The direct port's sign-in (direct port login design §3.2, §3.5; step 2 of §7, 20 Nov 2026).

On the direct port every request needs EMT's session cookie, apart from the sign-in's pages and static
files; a page without one goes to /signin, an API answers 401, and a write with one must come from EMT's
own page (Origin). Ingress is untouched. The cookie is signed with a secret kept in its own file, so a
restart keeps everyone signed in, and a backup (blocks.db only) never carries it. The sign-in finishes
with Home Assistant (code -> token -> auth/current_user -> revoke) and admits administrators only; a cookie
the browser did not keep ends on a page that says why, not on another sign-in.

The routes run on a bare Flask app with the blueprint and the guard; `signin.finish` (the calls to Home
Assistant, checked on a real install, design §6) is replaced. Every test here fails on the parent, which
has no sign-in: there, every request on the direct port goes through.
"""
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))
from flask import Flask                     # noqa: E402
from web import signin                      # noqa: E402

HOST = "emt.test:8098"


def _app():
    app = Flask(__name__)
    app.register_blueprint(signin.bp)
    app.before_request(signin.guard)

    @app.route("/data-management")
    def page():
        return "the page"

    @app.route("/api/thing", methods=["GET", "POST"])
    def thing():
        return {"ok": True}

    @app.route("/static/x.css")
    def css():
        return "css"

    @app.route("/api/energy", methods=["GET", "POST"])
    def energy():
        return {"ok": True}
    return app


class _Base(unittest.TestCase):
    mode = "supervised"
    login = ""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._cfg = dict(signin._cfg)
        signin.configure(self.dir, mode=self.mode, login=self.login)
        self.app = _app()
        self.c = self.app.test_client()
        self.base = {"REMOTE_ADDR": "192.168.1.20"}

    def tearDown(self):
        signin._cfg.update(self._cfg)
        signin._pending.clear()

    # Each request carries exactly the cookie given, as a header: never the test client's jar, which kept an
    # earlier cookie beside a later one and sent them in an order that differs between Werkzeug versions
    # (CI, 20 Nov 2026: a cookie signed with another secret "passed" on the valid one sent with it).
    def _client(self):
        return self.app.test_client(use_cookies=False)

    def get(self, path, cookie=None, **env):
        h = {"Cookie": "%s=%s" % (signin.COOKIE, cookie)} if cookie is not None else {}
        return self._client().get(path, base_url="http://" + HOST, headers=h, environ_base=dict(self.base, **env))

    def post(self, path, origin=None, cookie=None):
        h = {"Origin": origin} if origin else {}
        if cookie is not None:
            h["Cookie"] = "%s=%s" % (signin.COOKIE, cookie)
        return self._client().post(path, base_url="http://" + HOST, headers=h, environ_base=self.base)


class TheGuard(_Base):

    def test_a_page_without_a_session_goes_to_signin(self):
        r = self.get("/data-management?x=1")
        self.assertEqual(r.status_code, 302)
        loc = urlsplit(r.headers["Location"])
        self.assertEqual(loc.path, "/signin")
        self.assertEqual(parse_qs(loc.query)["next"], ["/data-management?x=1"])

    def test_an_api_without_a_session_answers_401(self):
        self.assertEqual(self.get("/api/thing").status_code, 401)
        self.assertEqual(self.post("/api/thing", origin="http://" + HOST).status_code, 401)

    def test_ingress_is_untouched(self):
        self.assertEqual(self.get("/data-management", **{"emt.ingress": True}).status_code, 200)
        self.assertEqual(self.c.post("/api/thing", base_url="http://" + HOST,
                                     environ_base=dict(self.base, **{"emt.ingress": True})).status_code, 200)

    def test_the_signin_pages_and_static_need_no_session(self):
        with mock.patch.object(signin, "ha_public_url", return_value="http://ha.test:8123"):
            for path in ("/signin", "/signin/check", "/static/x.css"):
                self.assertEqual(self.get(path).status_code, 200, path)

    def test_a_valid_session_passes_and_a_forged_one_does_not(self):
        good = signin.make_session("u1")
        self.assertEqual(self.get("/data-management", cookie=good).status_code, 200)
        body, sig = good.rsplit(".", 1)
        forged = body + "." + ("A" if sig[0] != "A" else "B") + sig[1:]      # the signature's first character
        self.assertEqual(self.get("/data-management", cookie=forged).status_code, 302)
        other = signin.URLSafeTimedSerializer(b"x" * 64, salt="emt-direct-session").dumps({"u": "u1"})
        self.assertEqual(self.get("/data-management", cookie=other).status_code, 302)

    def test_a_session_expires_after_30_days(self):
        good = signin.make_session("u1")
        with mock.patch("itsdangerous.timed.time.time", return_value=__import__("time").time() + signin.MAX_AGE + 5):
            self.assertEqual(self.get("/data-management", cookie=good).status_code, 302)

    def test_a_write_needs_emts_own_origin(self):
        good = signin.make_session("u1")
        self.assertEqual(self.post("/api/thing", origin="http://" + HOST, cookie=good).status_code, 200)
        self.assertEqual(self.post("/api/thing", origin="http://emt.test:8123", cookie=good).status_code, 403)
        self.assertEqual(self.post("/api/thing", origin="http://evil.test", cookie=good).status_code, 403)
        self.assertEqual(self.post("/api/thing", cookie=good).status_code, 403)          # no Origin at all

    def test_signing_out_is_a_write_and_clears_the_cookie(self):
        good = signin.make_session("u1")
        self.assertEqual(self.post("/signin/out", origin="http://evil.test", cookie=good).status_code, 403)
        r = self.post("/signin/out", origin="http://" + HOST, cookie=good)
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.headers["Set-Cookie"], r"emt_session=;.*(Max-Age=0|Expires=Thu, 01 Jan 1970)")


class TheSecret(_Base):

    def test_it_survives_a_restart_and_is_private(self):
        first = signin._cfg["secret"]
        cookie = signin.make_session("u1")
        signin.configure(self.dir, mode="supervised", login="")             # a restart
        self.assertEqual(signin._cfg["secret"], first)
        self.assertIsNotNone(signin.read_session(cookie))
        mode = stat.S_IMODE(os.stat(os.path.join(self.dir, signin.SECRET_FILE)).st_mode)
        self.assertEqual(mode, 0o600)

    def test_it_is_not_in_the_database(self):
        """A backup is blocks.db only (server._create_backup_zip), so the secret is a file of its own."""
        self.assertEqual(os.listdir(self.dir), [signin.SECRET_FILE])


class TheSignin(_Base):

    def _start(self, nxt="/data-management"):
        with mock.patch.object(signin, "ha_public_url", return_value="http://ha.test:8123"):
            r = self.get("/signin/start?next=" + nxt)
        loc = urlsplit(r.headers["Location"])
        return loc, parse_qs(loc.query)

    def test_start_sends_the_browser_to_home_assistant(self):
        loc, q = self._start()
        self.assertEqual("%s://%s%s" % (loc.scheme, loc.netloc, loc.path), "http://ha.test:8123/auth/authorize")
        self.assertEqual(q["client_id"], ["http://%s/" % HOST])
        self.assertEqual(q["redirect_uri"], ["http://%s/signin/done" % HOST])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertTrue(q["state"][0] and q["code_challenge"][0])

    # 20 Nov 2026, the user's staging: Home Assistant's local address said https://…:8123, Home Assistant served
    # plain HTTP there, and the browser could not connect. The page tries a short list from the browser; the
    # server sends it only to one of that list, by its place

    def test_the_addresses_tried_after_home_assistants_own(self):
        with mock.patch.object(signin, "ha_public_url", return_value="https://emt.test:8123"):
            with self.app.test_request_context("/signin", base_url="http://" + HOST):
                self.assertEqual(signin.ha_addresses(), ["https://emt.test:8123", "http://emt.test:8123"])
        with mock.patch.object(signin, "ha_public_url", return_value="http://ha.lan:8123"):
            with self.app.test_request_context("/signin", base_url="http://" + HOST):
                self.assertEqual(signin.ha_addresses(),
                                 ["http://ha.lan:8123", "http://emt.test:8123", "https://emt.test:8123"])

    def test_the_page_tries_each_and_takes_the_first_that_answers(self):
        with mock.patch.object(signin, "ha_public_url", return_value="https://emt.test:8123"):
            body = self.get("/signin?next=/charts").get_data(as_text=True)
        self.assertIn('var a=["https://emt.test:8123", "http://emt.test:8123"]', body)
        self.assertIn("document.getElementById('go').href='/signin/start?'+q+'&ha='+i;", body)
        self.assertIn("href='/signin/start?next=%2Fcharts'", body)        # without script: the first, as before

    def test_start_goes_only_to_a_place_in_its_own_list(self):
        def to(ha):
            with mock.patch.object(signin, "ha_public_url", return_value="https://emt.test:8123"):
                loc = urlsplit(self.get("/signin/start?next=/&ha=" + ha).headers["Location"])
            return "%s://%s" % (loc.scheme, loc.netloc)
        self.assertEqual(to("1"), "http://emt.test:8123")
        for bad in ("0", "2", "-1", "x", "http%3A%2F%2Fevil.test"):
            self.assertEqual(to(bad), "https://emt.test:8123", bad)

    def test_nothing_in_an_address_can_close_the_script(self):
        self.assertNotIn("<", signin._js(["http://a</script><script>alert(1)//"]))

    def _done(self, user=None, exc=None, state=None):
        loc, q = self._start()

        async def fake(code, verifier, client_id):
            self.assertEqual(client_id, "http://%s/" % HOST)
            if exc:
                raise exc
            return user
        with mock.patch.object(signin, "finish", fake):
            return self.get("/signin/done?code=c&state=" + (state or q["state"][0]))

    def test_an_administrator_gets_a_session_and_is_checked(self):
        r = self._done(user={"id": "u1", "is_admin": True})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("/signin/check?"))
        cookie = re.search(r"emt_session=([^;]+)", r.headers["Set-Cookie"]).group(1)
        self.assertEqual(signin.read_session(cookie)["u"], "u1")
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", r.headers["Set-Cookie"])

    def test_a_user_who_is_not_an_administrator_is_refused(self):
        r = self._done(user={"id": "u2", "is_admin": False})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("Set-Cookie", r.headers)

    def test_home_assistant_refusing_is_said_and_no_session(self):
        r = self._done(exc=signin.SigninRefused("forbidden"))
        self.assertEqual(r.status_code, 403)
        self.assertIn(b"refused this sign-in", r.data)
        self.assertNotIn("Set-Cookie", r.headers)

    def test_the_page_says_only_emts_own_words(self):
        """GUARD (CodeQL, 20 Nov 2026): a refusal shows a fixed message, never the exception's text."""
        r = self._done(exc=signin.SigninRefused("<b>not a reason</b>"))
        self.assertEqual(r.status_code, 403)
        self.assertNotIn(b"not a reason", r.data)
        self.assertIn(signin.REFUSED["not_accepted"].encode(), r.data)

    def test_a_bad_or_reused_state_is_refused(self):
        self.assertEqual(self._done(user={"id": "u1", "is_admin": True}, state="nope").status_code, 400)
        loc, q = self._start()
        st = q["state"][0]

        async def fake(*a):
            return {"id": "u1", "is_admin": True}
        with mock.patch.object(signin, "finish", fake):
            self.assertEqual(self.get("/signin/done?code=c&state=" + st).status_code, 302)
            self.assertEqual(self.get("/signin/done?code=c&state=" + st).status_code, 400)

    def test_a_cookie_not_kept_ends_on_a_page_not_another_signin(self):
        r = self.get("/signin/check?next=/data-management")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Open EMT in a new tab", r.data)
        r = self.get("/signin/check?next=/data-management", cookie=signin.make_session("u1"))
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/data-management"))

    def test_next_never_leaves_emt(self):
        for bad in ("//evil.test/x", "https://evil.test/", "evil", "/\\evil.test", ""):
            self.assertEqual(signin.safe_next(bad), "/", bad)
        self.assertEqual(signin.safe_next("/charts?tab=billing"), "/charts?tab=billing")
        loc, q = self._start(nxt="//evil.test/")
        self.assertEqual(signin._pending[q["state"][0]]["next"], "/")


class Standalone(_Base):
    mode = "standalone"

    def test_signin_applies_and_the_supervisor_address_is_not_ingress(self):
        self.assertEqual(self.get("/data-management", REMOTE_ADDR="172.30.32.2").status_code, 302)

    def test_home_assistant_address_for_the_browser(self):
        with self.app.test_request_context("/signin", base_url="http://192.168.1.10:8098"):
            with mock.patch.dict(os.environ, {"HA_URL": "http://homeassistant:8123", "HA_PUBLIC_URL": ""}):
                self.assertEqual(signin.ha_public_url(), "http://192.168.1.10:8123")
            with mock.patch.dict(os.environ, {"HA_URL": "http://homeassistant:8123",
                                              "HA_PUBLIC_URL": "http://ha.example.lan:8123/"}):
                self.assertEqual(signin.ha_public_url(), "http://ha.example.lan:8123")
        with mock.patch.dict(os.environ, {"HA_URL": "http://homeassistant:8123/"}):
            self.assertEqual(signin.ha_internal_url(), "http://homeassistant:8123")


class StandaloneOff(_Base):
    mode = "standalone"
    login = "off"

    def test_the_way_out_admits_every_request(self):
        self.assertFalse(signin.required())
        self.assertEqual(self.get("/data-management").status_code, 200)
        self.assertEqual(self.post("/api/thing").status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.dir, signin.SECRET_FILE)))


class AnAddonCannotTurnItOff(_Base):
    login = "off"

    def test_off_is_ignored_on_an_addon(self):
        self.assertTrue(signin.required())
        self.assertEqual(self.get("/data-management").status_code, 302)


class ThePreview(unittest.TestCase):
    """.build/serve_local.py, bound to 127.0.0.1 with no Home Assistant to sign in to: off unless asked.
    (#169 switched the sign-in on in server.init(), which the preview also calls: it asked for a sign-in it
    could never finish.)"""

    def setUp(self):
        self._cfg = dict(signin._cfg)

    def tearDown(self):
        signin._cfg.update(self._cfg)

    def test_off_unless_asked(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"EMT_PREVIEW_SIGNIN": ""}):
            self.assertFalse(signin.configure(d, mode="supervised", login="", preview=True))
        with mock.patch.dict(os.environ, {"EMT_PREVIEW_SIGNIN": "1"}):
            self.assertTrue(signin.configure(d, mode="supervised", login="", preview=True))


class NoCardKey(_Base):
    """The dashboard cards are gone (20 Nov 2026, before b27 shipped them): no key in an address opens
    anything on the direct port. Discriminating: on the parent, a `card_key` file beside the secret let
    `?key=` read /api/energy with no session; an install that made one keeps the file, and it opens nothing."""

    def test_a_key_in_the_address_opens_nothing(self):
        key = "k" * 43
        with open(os.path.join(self.dir, "card_key"), "w", encoding="ascii") as f:
            f.write(key)
        self.assertEqual(self.get("/api/energy?resolution=day&key=" + key).status_code, 401)
        self.assertEqual(self.get("/data-management?key=" + key).status_code, 302)


class TheServerWiring(unittest.TestCase):

    def test_the_guard_runs_first_and_init_switches_it_on(self):
        from tests.test_server import server
        hooks = server.app.before_request_funcs[None]
        self.assertIs(hooks[0], server._signin.guard)
        self.assertIn("signin", server.app.blueprints)
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        init = src[src.index("def init(data_dir"):src.index("def _run_on_engine_loop")]
        self.assertIn("_signin.configure(data_dir, preview=preview)", init)
        local = open(os.path.join(_ROOT, ".build", "serve_local.py"), encoding="utf-8").read()
        self.assertIn("server.init(data, None, preview=True)", local)
        main = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
        self.assertIn("server.init(DATA_DIR, ha)", main)


if __name__ == "__main__":
    unittest.main()
