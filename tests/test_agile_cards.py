"""
Overview's Agile cards (6 Nov 2026, the user): the prices that never came, a card each for import and export, and
expand and hide.

The prices: `target_through` wanted prices to midnight, and Agile's day ends at 23:00, so the record was always short.
A real record showed it: `asked` 04:30, `tries` 4, prices to 23:00 today. The four tries a day went at 00:00, 01:30,
03:00 and 04:30, each on calls for nothing new, and none was left for tomorrow's at 4pm. BL-103's test had not seen it
because its stub Octopus answered to whatever `period_to` it was asked; `_Octopus` here stops where Octopus stops.

Export stopping, also reported, was not reproduced: the kept export prices in two support files have no holes.

Discriminating: the tests not marked GUARD fail on the parent. The page was also driven by hand in a browser.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import agile_public as A                         # noqa: E402

LON = ZoneInfo("Europe/London")
UTC = timezone.utc
PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()
SERVER = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()


def _day(rec, day, *, published_at):
    """The local times `due` asks on `day`. Octopus's next day (to 23:00 tomorrow) is out from `published_at`."""
    asks = []
    t = datetime(day.year, day.month, day.day, tzinfo=LON)
    while t.date() == day:
        u = t.astimezone(UTC).replace(tzinfo=None)
        if A.due(rec, u, t, LON, ["import"]):
            asks.append(t.strftime("%H:%M"))
            rec["asked"] = u.isoformat()
            k = t.date().isoformat()
            rec["tries"] = {k: int((rec.get("tries") or {}).get(k, 0)) + 1}
            if (t.hour, t.minute) >= published_at:
                end = datetime(day.year, day.month, day.day, 22, 30, tzinfo=LON) + timedelta(days=1)
                rec["import"]["prices"] = [[end.astimezone(UTC).replace(tzinfo=None).isoformat(), 10.0]]
        t += timedelta(minutes=10)
    return asks


class TheDay(unittest.TestCase):

    def _held_to_2300_today(self, day):
        end = datetime(day.year, day.month, day.day, 22, 30, tzinfo=LON).astimezone(UTC).replace(tzinfo=None)
        return {"import": {"prices": [[end.isoformat(), 10.0]]}}

    def test_nothing_until_tomorrows_are_out_then_one_ask(self):
        day = datetime(2026, 11, 6).date()
        self.assertEqual(_day(self._held_to_2300_today(day), day, published_at=(16, 0)), ["16:30"])

    def test_late_prices_are_asked_for_again(self):
        day = datetime(2026, 11, 6).date()
        self.assertEqual(_day(self._held_to_2300_today(day), day, published_at=(19, 0)), ["16:30", "18:00", "19:30"])

    def test_the_same_in_summer_time(self):
        day = datetime(2026, 7, 6).date()
        self.assertEqual(_day(self._held_to_2300_today(day), day, published_at=(16, 0)), ["16:30"])

    def test_after_23_00_it_still_wants_tomorrows_not_today(self):
        late = datetime(2026, 11, 6, 23, 30, tzinfo=LON)
        self.assertEqual(A.target_through(late, LON), datetime(2026, 11, 7, 23, 0))


class _Octopus:
    """Answers as Octopus does: nothing past `through`, whatever `period_to` asks. `fail` raises for one tariff."""

    def __init__(self, through, fail=None):
        self.through, self.fail, self.asked = through, fail, []

    async def get_products(self):
        return [{"code": "AGILE-24-10-01", "available_from": "2024-10-01T00:00:00Z"},
                {"code": "AGILE-OUTGOING-19-05-13", "available_from": "2019-05-13T00:00:00Z"}]

    async def get_unit_rates(self, product, tariff, period_from=None, period_to=None):
        self.asked.append(tariff)
        if self.fail and self.fail in tariff:
            raise RuntimeError("timed out")
        t, out = datetime.fromisoformat(period_from[:19]), []
        while t < min(self.through, datetime.fromisoformat(period_to[:19])):
            out.append({"valid_from": t.isoformat() + "Z", "value_inc_vat": 15.0})
            t += timedelta(minutes=30)
        return out


class _Engine(unittest.TestCase):

    def setUp(self):
        import engine
        from block_store import BlockStore
        self.engine = engine
        self.st = BlockStore(":memory:")
        self.st._conn.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, block_minutes, "
                              "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'Europe/London')")
        self.st._conn.commit()
        self.disc = {"import": {"tariff_code": "E-1R-IOG-SMB-FIX-12M-26-03-17-C"},
                     "export": {"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-C"}}
        self.p = [mock.patch.object(engine, "_store", self.st), mock.patch.object(engine, "_reader", lambda: self.st),
                  mock.patch.object(engine, "_kraken_discovery", self.disc),
                  # `_agile_public_due` says no without a client: with none, these tests would pass for that reason
                  mock.patch.object(engine, "_kraken_client", _Octopus(datetime(2030, 1, 1)))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.st.close()

    def _fetch(self, oct_):
        with mock.patch.object(self.engine, "_kraken_client", oct_):
            return asyncio.run(self.engine._fetch_agile_public())


class TheFetch(_Engine):

    def _agile_end(self):
        """Where Octopus's prices stop right now: 23:00 local, tomorrow's once they are out. Worked out here,
        not read from `target_through`: that is the rule under test."""
        now = datetime.now(LON)
        day = now.date() + timedelta(days=1 if (now.hour, now.minute) >= (16, 30) else 0)
        end = datetime(day.year, day.month, day.day, 23, tzinfo=LON)
        return end.astimezone(UTC).replace(tzinfo=None)

    def test_prices_that_stop_where_agile_stops_are_enough(self):
        self._fetch(_Octopus(self._agile_end()))
        self.assertFalse(self.engine._agile_public_due())
        rec = self.st.get_meta(A.META_KEY)
        rec["asked"] = "2020-01-01T00:00:00"          # past the 90 minutes: short is what would make it due
        self.st.set_meta(A.META_KEY, rec)
        self.assertFalse(self.engine._agile_public_due())

    def test_a_failed_direction_counts_as_a_try_and_the_other_is_kept(self):
        self._fetch(_Octopus(self._agile_end(), fail="OUTGOING"))
        rec = self.st.get_meta(A.META_KEY)
        self.assertEqual(sum(rec["tries"].values()), 1)
        self.assertTrue(rec["import"]["prices"])
        self.assertFalse(rec["export"].get("prices"))


class Hiding(_Engine):

    def _hide(self, **kw):
        self.st.save_settings(kw)

    def test_a_hidden_direction_is_not_fetched(self):
        self._hide(agile_hide_export=True)
        self.assertEqual(self.engine._agile_public_channels(), ["import"])
        oct_ = _Octopus(datetime(2030, 1, 1))
        self._fetch(oct_)
        self.assertEqual(oct_.asked, ["E-1R-AGILE-24-10-01-C"])

    def test_both_hidden_nothing_is_due(self):
        self._hide(agile_hide_import=True, agile_hide_export=True)
        self.assertEqual(self.engine._agile_public_channels(), [])
        self.assertFalse(self.engine._agile_public_due())

    def test_shown_again_it_picks_up_from_what_it_holds(self):
        through = A.target_through(datetime.now(LON), LON)
        self._fetch(_Octopus(through))
        self._hide(agile_hide_export=True)
        rec = self.st.get_meta(A.META_KEY)
        rec["export"]["prices"] = rec["export"]["prices"][:-96]       # two days behind while hidden
        rec["asked"] = "2020-01-01T00:00:00"
        rec["tries"] = {}
        self.st.set_meta(A.META_KEY, rec)
        self.assertFalse(self.engine._agile_public_due())
        self._hide(agile_hide_export=False)
        self.assertTrue(self.engine._agile_public_due())
        oct_ = _Octopus(through)
        self._fetch(oct_)
        self.assertEqual(oct_.asked, ["E-1R-AGILE-OUTGOING-19-05-13-C"])
        self.assertEqual(A.through_of(self.st.get_meta(A.META_KEY)["export"]["prices"]), through)

    def test_GUARD_a_settings_read_that_fails_does_not_stop_the_tick(self):
        class Bad:
            def get_settings(self):
                raise RuntimeError("locked")
        with mock.patch.object(self.engine, "_reader", lambda: Bad()):
            self.assertEqual(self.engine._agile_public_channels(), ["import", "export"])


class TheRoute(unittest.TestCase):

    def test_settings_know_the_keys_so_the_post_keeps_them(self):
        self.assertIn('SETTINGS_FLAGS = {"agile_hide_import": False, "agile_hide_export": False}', SERVER)
        self.assertIn("for key in list(SETTINGS_DEFAULTS) + list(SETTINGS_FLAGS):", SERVER)
        self.assertIn("result = dict(SETTINGS_DEFAULTS, **SETTINGS_FLAGS)", SERVER)

    def test_the_route_leaves_a_hidden_direction_out(self):
        i = SERVER.index("def api_agile_prices(")
        body = SERVER[i:i + 2500]
        self.assertIn("if ch in hidden:\n                out.pop(ch, None)", body)

    def test_the_page_is_told_before_it_draws(self):
        self.assertIn("agile_hidden=agile_hidden,", SERVER)
        self.assertIn("{{ agile_hidden|default(", PAGE)
        self.assertIn("{% if agile_hidden[ch] or ch == 'export' %} style=\"display:none;\"{% endif %}", PAGE)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheCards(unittest.TestCase):

    def _run(self, expr):
        def fn(n):
            i = PAGE.index("function " + n + "(")
            depth, j = 0, PAGE.index("{", i)
            while True:
                depth += {"{": 1, "}": -1}.get(PAGE[j], 0)
                if PAGE[j] == "}" and depth == 0:
                    return PAGE[i:j + 1]
                j += 1
        js = "\n".join(fn(n) for n in ("_agileMs", "_agileHHMM", "_agileCheapest", "_agileShown", "_agileExportColor",
                                           "_agileStatus", "_agileWhen")) \
            + "\nconsole.log(JSON.stringify(" + expr + "));"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(js)
            r = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    P = "{prices: [['2026-11-01T10:00:00', 1]]}"

    def _shown(self, data, hidden="{}"):
        return self._run("_agileShown(%s, %s)" % (data, hidden))

    def test_both_with_prices_show_two_cards(self):
        self.assertEqual(self._shown("{'import': %s, 'export': %s}" % (self.P, self.P)), {"import": True, "export": True})

    def test_no_export_prices_no_export_card(self):
        self.assertEqual(self._shown("{'import': %s}" % self.P), {"import": True, "export": False})

    def test_only_export_has_prices_no_empty_import_card(self):
        self.assertEqual(self._shown("{'export': %s}" % self.P), {"import": False, "export": True})

    def test_no_prices_at_all_import_alone_says_so(self):
        self.assertEqual(self._shown("{}"), {"import": True, "export": False})

    def test_hidden_stays_hidden(self):
        self.assertEqual(self._shown("{'import': %s, 'export': %s}" % (self.P, self.P), "{'import': true}"),
                         {"import": False, "export": True})
        self.assertEqual(self._shown("{}", "{'import': true, 'export': true}"), {"import": False, "export": False})

    def _status(self, last, now):
        return self._run("_agileStatus([['%s', 1]], _agileMs('%s'))" % (last, now))

    def test_prices_reaching_the_charts_edge_mean_tomorrows_are_in(self):
        # sent to 17:00 tomorrow only because the chart stops 24 h ahead: not "Published to 17:30"
        self.assertEqual(self._status("2026-11-07T17:00:00", "2026-11-06T17:14:00"), "Tomorrow\u2019s published")

    def test_prices_short_of_the_edge_say_when_tomorrows_come(self):
        out = self._status("2026-11-06T22:30:00", "2026-11-06T09:00:00")
        self.assertIn("tomorrow\u2019s from about 4pm", out)
        self.assertTrue(out.startswith("Published to "))

    def test_an_hour_tomorrow_says_so(self):
        now = "_agileMs('2026-11-06T12:00:00')"
        self.assertTrue(self._run("_agileWhen(_agileMs('2026-11-07T12:00:00'), %s)" % now).endswith(" tomorrow"))
        self.assertFalse(self._run("_agileWhen(_agileMs('2026-11-06T18:00:00'), %s)" % now).endswith(" tomorrow"))

    def test_export_looks_for_its_best_hour(self):
        prices = [["2026-11-01T11:00:00", 5], ["2026-11-01T11:30:00", 7], ["2026-11-01T12:00:00", 20]]
        self.assertEqual(self._run("_agileCheapest(%s, _agileMs('2026-11-01T10:52:00'), true).avg" % json.dumps(prices)), 13.5)

    def test_export_runs_blue_to_purple_and_red_below_zero(self):
        self.assertEqual(self._run("[_agileExportColor(0, 0, 30), _agileExportColor(30, 0, 30), _agileExportColor(-1, -1, 30)]"),
                         ["hsl(205, 62%, 54%)", "hsl(275, 62%, 46%)", "#ef4444"])


class ThePage(unittest.TestCase):

    def test_a_card_each_with_expand_and_hide(self):
        self.assertIn("{% for ch in ('import', 'export') %}", PAGE)
        self.assertIn("""onclick="_expandCard('agile-{{ ch }}')\"""", PAGE)
        self.assertIn("""onclick="_agileSetHidden('{{ ch }}', true)\"""", PAGE)

    def test_expand_moves_the_real_canvas_and_gives_it_back(self):
        self.assertIn("var cardId = agileCh ? 'agile-card-' + agileCh :", PAGE)
        self.assertIn("_agileOrigWrap = acv.parentElement;", PAGE)
        self.assertIn("_agileOrigWrap.appendChild(aCv);", PAGE)

    def test_the_foot_of_the_overview_brings_them_back(self):
        i = PAGE.index('id="agile-restore"')
        self.assertLess(PAGE.index("</div><!-- /.live-power-page -->"), i)
        for ch in ("import", "export"):
            self.assertIn("_agileSetHidden('%s', false)" % ch, PAGE[i:i + 700])


if __name__ == "__main__":
    unittest.main()
