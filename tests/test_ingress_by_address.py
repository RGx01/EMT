"""
Ingress told by the Supervisor's address, not by a header (direct port login design §3.1, 19 Nov 2026).

Home Assistant's ingress reaches an add-on only from the Supervisor, 172.30.32.2, which sets
X-Ingress-Path and the X-Remote-User-* headers itself. EMT read X-Ingress-Path from any client: on the
direct port anyone could send it, and its value became SCRIPT_NAME, written into every page's links.
Now the headers count only from the Supervisor on an add-on install; from anywhere else, and from
anything standalone, they are dropped before the app sees them.

Discriminating: test_a_direct_request_cannot_claim_ingress, test_standalone_has_no_ingress and
test_the_user_headers_are_dropped_off_the_supervisor fail on the parent, which honoured the header from
any address. test_the_supervisor_is_ingress and test_a_plain_direct_request_is_unchanged are GUARDS in
substance: the parent rewrote those requests the same way, and fails them only for want of
`emt.ingress`.
"""
import logging
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))
from tests.test_server import server     # noqa: E402

LAN = "192.168.1.20"


def _through(remote_addr, headers=None, path="/api/hassio_ingress/abc/data-management", mode="supervised"):
    """The environ the app sees after the middleware, for one request."""
    seen = {}

    def app(environ, start_response):
        seen.update(environ)
        return []

    env = {"REMOTE_ADDR": remote_addr, "PATH_INFO": path, "SCRIPT_NAME": ""}
    env.update(headers or {})
    with mock.patch.dict(os.environ, {"EMT_MODE": mode}):
        server.IngressMiddleware(app)(env, lambda *a: None)
    return seen


INGRESS = {"HTTP_X_INGRESS_PATH": "/api/hassio_ingress/abc",
           "HTTP_X_REMOTE_USER_ID": "u1", "HTTP_X_REMOTE_USER_NAME": "jane",
           "HTTP_X_REMOTE_USER_DISPLAY_NAME": "Jane"}


class IngressByAddress(unittest.TestCase):

    def test_the_supervisor_is_ingress(self):
        env = _through(server.SUPERVISOR_ADDR, INGRESS)
        self.assertTrue(env["emt.ingress"])
        self.assertEqual(env["SCRIPT_NAME"], "/api/hassio_ingress/abc")
        self.assertEqual(env["PATH_INFO"], "/data-management")
        self.assertEqual(env["HTTP_X_REMOTE_USER_ID"], "u1")

    def test_a_direct_request_cannot_claim_ingress(self):
        for peer in (LAN, "172.30.32.1", "127.0.0.1", ""):
            env = _through(peer, INGRESS)
            self.assertFalse(env["emt.ingress"], peer)
            self.assertEqual(env["SCRIPT_NAME"], "", peer)                 # no prefix in the links
            self.assertEqual(env["PATH_INFO"], "/api/hassio_ingress/abc/data-management", peer)
            self.assertNotIn("HTTP_X_INGRESS_PATH", env, peer)

    def test_the_user_headers_are_dropped_off_the_supervisor(self):
        env = _through(LAN, INGRESS)
        for h in ("HTTP_X_REMOTE_USER_ID", "HTTP_X_REMOTE_USER_NAME", "HTTP_X_REMOTE_USER_DISPLAY_NAME"):
            self.assertNotIn(h, env)

    def test_standalone_has_no_ingress(self):
        """A standalone Docker network can use any range: even 172.30.32.2 is a direct request."""
        env = _through(server.SUPERVISOR_ADDR, INGRESS, mode="standalone")
        self.assertFalse(env["emt.ingress"])
        self.assertEqual(env["SCRIPT_NAME"], "")
        self.assertNotIn("HTTP_X_REMOTE_USER_ID", env)

    def test_a_plain_direct_request_is_unchanged(self):
        """GUARD: the dashboard card's request, with no ingress headers, reaches the app as before."""
        env = _through(LAN, path="/lovelace/heatmap")
        self.assertFalse(env["emt.ingress"])
        self.assertEqual((env["SCRIPT_NAME"], env["PATH_INFO"]), ("", "/lovelace/heatmap"))


class ThePeerInTheLog(unittest.TestCase):

    def test_each_kind_logged_once_without_a_lan_address(self):
        """Login design §6 check 1: which address the direct port's requests come from on a real install.
        Logged once a kind per start, and a user's LAN address is never written."""
        mw = server.IngressMiddleware(lambda e, s: [])
        with self.assertLogs(server.__name__, logging.INFO) as cm:
            for peer in (server.SUPERVISOR_ADDR, server.SUPERVISOR_ADDR, LAN, LAN, "172.30.32.1"):
                with mock.patch.dict(os.environ, {"EMT_MODE": "supervised"}):
                    mw({"REMOTE_ADDR": peer, "PATH_INFO": "/"}, lambda *a: None)
        lines = [r.getMessage() for r in cm.records if "first" in r.getMessage()]
        self.assertEqual(lines, ["server: first ingress request from the Supervisor",
                                 "server: first direct request from another address",
                                 "server: first direct request from the add-on network (172.30.32.1)"])
        self.assertFalse(any(LAN in l for l in lines))


if __name__ == "__main__":
    unittest.main()
