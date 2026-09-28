"""`tmc open`, `tmc catalog` and `tmc defcon` against the mock.

All three reach surfaces that take no credential (the anonymous summary, the
app API's public reads, the status page's tRPC), so every test here also checks
that the profile's token was NOT sent — `run_cli` puts `--token` on the line
precisely so that a transport which leaked it would be caught.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from mock_server import STATE  # noqa: E402
from test_cli import CliTestCase  # noqa: E402

from tmc_cli.config import site_url  # noqa: E402


class _Args:
    site_url = None
    profile = None


class TestSiteUrl(unittest.TestCase):
    def test_api_host_maps_to_the_site(self) -> None:
        with mock.patch.dict(os.environ, {"TMC_SITE_URL": ""}):
            self.assertEqual(
                site_url(_Args(), "https://api.moddingcommunity.com"),
                "https://moddingcommunity.com",
            )
            self.assertEqual(site_url(_Args(), "http://localhost:3000"), "http://localhost:3000")

    def test_userinfo_is_dropped_and_port_kept(self) -> None:
        with mock.patch.dict(os.environ, {"TMC_SITE_URL": ""}):
            self.assertEqual(
                site_url(_Args(), "https://api.example.com@api.example.com:8443/x"),
                "https://example.com:8443",
            )

    def test_env_wins(self) -> None:
        with mock.patch.dict(os.environ, {"TMC_SITE_URL": "https://staging.example/"}):
            self.assertEqual(site_url(_Args(), "https://api.x.com"), "https://staging.example")


class TestOpen(CliTestCase):
    def test_keyed_record_uses_the_sites_own_path(self) -> None:
        # The keyed record's `url` is the SLUG. Before the fix `open` printed
        # "cool-mod" and refused to hand it to a browser.
        item = self.seed_mod(url="cool-mod")

        with mock.patch("webbrowser.open") as launch:
            out = self.run_cli("open", "mod", str(item), "--browser").strip()

        self.assertEqual(out, f"{self.server.base_url}/mod/cool-mod")
        launch.assert_called_once_with(out)

    def test_hidden_record_falls_back_to_a_built_path(self) -> None:
        item = self.seed_mod(url="secret", hidden=True)
        out = self.run_cli("open", "mod", str(item)).strip()

        self.assertEqual(out, f"{self.server.base_url}/1/m/secret")

    def test_comment_uses_the_permalink_route(self) -> None:
        row = {"id": STATE.take_id(), "content": "hi", "modId": 1}
        STATE.rows["comment"][row["id"]] = row

        out = self.run_cli("open", "comment", str(row["id"]), "--site-url", "https://site.test").strip()

        self.assertEqual(out, f"https://site.test/i/comment/{row['id']}")

    def test_slug_is_quoted(self) -> None:
        item = self.seed_mod(url="a b\x1b]", hidden=True)
        out = self.run_cli("open", "mod", str(item)).strip()

        self.assertTrue(out.endswith("/1/m/a%20b%1B%5D"), out)


class TestCatalog(CliTestCase):
    def assert_no_credential(self) -> None:
        self.assertTrue(STATE.auth_headers)
        self.assertTrue(all(h is None for h in STATE.auth_headers), STATE.auth_headers)

    def test_browse_flattens_and_sends_repeated_keys(self) -> None:
        out = self.run_cli("catalog", "browse", "mod", "--tag", "1", "--tag", "2", "-o", "json")
        rows = json.loads(out)

        self.assertEqual([r["id"] for r in rows], ["1", "3"])
        self.assertIn("tags=1&tags=2", STATE.requests[-1][1])
        self.assert_no_credential()

        table = self.run_cli("catalog", "browse", "mod", "--field", "id,owner,tags", "-o", "csv")
        self.assertIn("1,ann,\"fun, pvp\"", table)

    def test_browse_follows_the_cursor(self) -> None:
        rows = json.loads(self.run_cli("catalog", "browse", "mod", "--limit", "2", "--all", "-o", "json"))
        self.assertEqual(len(rows), 4)

        capped = json.loads(
            self.run_cli("catalog", "browse", "mod", "--limit", "2", "--all", "--max", "3", "-o", "json")
        )
        self.assertEqual(len(capped), 3)

    def test_show_and_its_parts(self) -> None:
        row = json.loads(self.run_cli("catalog", "show", "mod", "1", "-o", "jsonl"))
        self.assertEqual((row["dependencies"], row["releases"], row["owner"]), (1, 1, "ann"))

        deps = json.loads(self.run_cli("catalog", "show", "mod", "1", "--part", "dependencies", "-o", "json"))
        self.assertEqual(deps[0]["name"], "Lib")

        err = self.run_cli_err("catalog", "show", "mod", "99", expect=4)
        self.assertIn("Not found.", err)
        self.assertNotIn("{'code'", err)

    def test_facets_reviews_games_lookup(self) -> None:
        facets = self.run_cli("catalog", "facets", "mod", "-o", "csv")
        self.assertIn("category,7,Maps,2", facets)

        reviews = json.loads(self.run_cli("catalog", "reviews", "mod", "1", "-o", "jsonl"))
        self.assertEqual((reviews["owner"], reviews["rating"]), ("ann", 5))

        games = json.loads(self.run_cli("catalog", "games", "-o", "jsonl"))
        self.assertEqual(games["slug"], "seed")

        servers = json.loads(self.run_cli("catalog", "lookup", "play.example.com", "--port", "27015", "-o", "json"))
        self.assertEqual([s["id"] for s in servers], ["5"])
        self.assert_no_credential()


class TestDefcon(CliTestCase):
    def test_status_summary_and_check(self) -> None:
        row = json.loads(self.run_cli("defcon", "status", "-o", "jsonl"))
        self.assertEqual(
            (row["overall"], row["monitors"], row["degraded"], row["openIncidents"]),
            ("DEGRADED", 3, 1, 1),
        )
        self.assertTrue(STATE.requests[-1][1].startswith("/api/trpc/defcon.public.status"))
        self.assertIsNone(STATE.auth_headers[-1])

        self.run_cli("defcon", "status", "--check", expect=1)
        STATE.defcon["overall"] = "OK"
        self.run_cli("defcon", "status", "--check", expect=0)

    def test_monitors_and_one_monitor(self) -> None:
        rows = json.loads(self.run_cli("defcon", "monitors", "--status", "OK", "-o", "jsonl").splitlines()[0])
        self.assertEqual(rows["name"], "API")

        table = self.run_cli("defcon", "monitors", "--kind", "web", "-o", "csv")
        self.assertIn("1,Homepage,WEB,example.com/,DEGRADED,80.2,99.5,99.9,99.95", table)

        nodes = self.run_cli("defcon", "monitor", "home", "-o", "csv")
        self.assertIn("Sydney,DEGRADED,820.5", nodes)

        self.run_cli_err("defcon", "monitor", "nope", expect=2)

    def test_nodes_and_incidents_honour_the_sites_switches(self) -> None:
        self.assertIn("Frankfurt", self.run_cli("defcon", "nodes"))
        self.assertIn("820 ms > 500 ms", self.run_cli("defcon", "incidents"))

        STATE.defcon["show"] = {"nodes": False, "incidents": False, "mtr": False}
        self.run_cli_err("defcon", "nodes", expect=2)
        self.run_cli_err("defcon", "incidents", expect=2)

        # The page hides node names, so the per-node rows fall back to ids.
        self.assertIn("2,DEGRADED", self.run_cli("defcon", "monitor", "1", "-o", "csv"))

    def test_status_off(self) -> None:
        STATE.defcon = {"enabled": False, "show": {}, "overall": "UNKNOWN", "nodes": [], "monitors": [], "openAlerts": []}
        err = self.run_cli_err("defcon", "monitors", expect=2)
        self.assertIn("does not publish", err)
        # Under --check a non-answer is UNKNOWN, never DOWN (2) or DEGRADED (1).
        self.run_cli("defcon", "status", "--check", expect=3)

    def test_check_down_and_unreachable(self) -> None:
        STATE.defcon["overall"] = "DOWN"
        self.run_cli("defcon", "status", "--check", expect=2)
        self.run_cli("defcon", "status", "--check", "--site-url", "http://127.0.0.1:9", expect=3)

    def test_error_text_is_defanged(self) -> None:
        for m in STATE.defcon["monitors"][:2]:
            m["name"] = "zzq\x1b]0;pwned\x07" + str(m["id"])
        err = self.run_cli_err("defcon", "monitor", "zzq", expect=2)
        self.assertIn("matches 2 monitors", err)
        self.assertNotIn("\x1b", err)

    def test_latency_series_and_summary(self) -> None:
        rows = self.run_cli("defcon", "latency", "Homepage", "--range", "week", "--node", "frankfurt", "-o", "csv")
        self.assertEqual(len(rows.strip().splitlines()), 3)

        sent = STATE.requests[-1][1]
        self.assertIn("defcon.public.series", sent)
        self.assertIn("%22range%22%3A+%22week%22", sent)

        summary = json.loads(self.run_cli("defcon", "latency", "1", "--summary", "-o", "json"))
        frankfurt = next(r for r in summary if r["node"] == "Frankfurt")
        self.assertEqual((frankfurt["latestMs"], frankfurt["meanMs"], frankfurt["peakMs"]), (44.0, 42.0, 90.0))

    def test_mtr(self) -> None:
        out = self.run_cli("defcon", "mtr", "Route", "-o", "csv")
        self.assertIn("10.0.0.1", out)

        err = self.run_cli_err("defcon", "mtr", "1", expect=0)
        self.assertIn("No traceroute", err)

    def test_dry_run_sends_nothing(self) -> None:
        self.run_cli("defcon", "monitors", "--dry-run")
        self.assertEqual(STATE.requests, [])


if __name__ == "__main__":
    unittest.main()
