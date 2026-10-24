"""`tmc open`, `tmc catalog` and `tmc defcon` against the mock.

All three reach surfaces that take no credential (the anonymous summary, the
app API's public reads, the status page's tRPC), so every test here also checks
that the profile's token was NOT sent — `run_cli` puts `--token` on the line
precisely so that a transport which leaked it would be caught.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(__file__))

from mock_server import STATE  # noqa: E402
from test_cli import CliTestCase  # noqa: E402

from tmc_cli import cli  # noqa: E402

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

    def test_build(self) -> None:
        build = json.loads(self.run_cli("catalog", "build", "1", "--platform", "LINUX_X64", "-o", "json"))
        self.assertEqual((build["appId"], build["format"], build["entry"]), (1, "TAR_GZ", "seed"))
        self.assertEqual(json.loads(self.run_cli("catalog", "build", "1", "--platform", "IOS_ARM64", "-o", "json")), None)
        self.assert_no_credential()

    def test_server_listing(self) -> None:
        first = json.loads(self.run_cli("catalog", "listing", "--app", "seed", "-o", "json"))
        self.assertEqual([r["id"] for r in first], [703, 704])
        self.assertEqual((first[0]["host"], first[0]["protocols"], first[0]["app"]["url"]), ("203.0.113.7", ["A2S"], "seed"))
        table = self.run_cli("catalog", "listing", "-o", "csv")
        self.assertIn("703,203.0.113.7,6060,,A2S,seed", table)

        every = json.loads(self.run_cli("catalog", "listing", "--all", "-o", "json"))
        self.assertEqual([r["id"] for r in every], [703, 704, 705])
        self.assert_no_credential()

        err = self.run_cli_err("catalog", "listing", "--app", "nope", expect=4)
        self.assertIn("No such game.", err)

    def test_facets_offline_maps_and_codes(self) -> None:
        rows = [json.loads(l) for l in self.run_cli("catalog", "facets", "server", "-o", "jsonl").splitlines()]
        by = {r["facet"]: r for r in rows}
        self.assertEqual((by["country"]["code"], by["map"]["code"], by["app"]["count"]), ("DE", "dm1", 5))
        self.assertNotIn("online=", STATE.requests[-1][1])

        offline = json.loads(self.run_cli("catalog", "facets", "server", "--offline", "-o", "json"))
        self.assertEqual(offline["apps"][0]["count"], 9)
        self.assertIn("online=false", STATE.requests[-1][1])
        self.assert_no_credential()

    def test_server_rows_carry_the_country_code(self) -> None:
        STATE.catalog_items[4]["server"] = {"country": "Germany", "countryCode": "DE"}
        table = self.run_cli("catalog", "lookup", "play.example.com", "-o", "csv")
        self.assertIn("country", table.splitlines()[0])
        self.assertIn(",DE,", table)

    def test_prefetch(self) -> None:
        packs = json.loads(self.run_cli("catalog", "prefetch", "7", "-o", "json"))
        self.assertEqual(packs["packs"][0]["id"], "seed/solo")
        self.assertEqual(STATE.requests[-1][1], "/api/app/v1/servers/7/prefetch")
        self.assertIn("seed/maps,0.3.0,8192", self.run_cli("catalog", "prefetch", "7", "-o", "csv"))
        self.assertIn("No such server.", self.run_cli_err("catalog", "prefetch", "8", expect=4))
        self.assert_no_credential()

    def test_avatar_schema_and_library(self) -> None:
        schema = json.loads(self.run_cli("catalog", "avatar-schema", "-o", "json"))
        self.assertEqual(schema["schema_id"], "backbone.v1")

        whole = json.loads(self.run_cli("catalog", "avatar-library", "--server", "7", "-o", "json"))
        self.assertEqual((whole["serverId"], whole["wearables"][0]["slot"]), (7, "hat"))
        self.assertEqual(STATE.requests[-1][1], "/api/avatar/v1/library?server=7")

        rows = self.run_cli("catalog", "avatar-library", "--app", "1", "-o", "csv")
        self.assertIn("wearables,9,Cap,1,300,wearable/9/1", rows)
        part = json.loads(self.run_cli("catalog", "avatar-library", "--app", "1", "--part", "actions", "-o", "json"))
        self.assertEqual(part[0]["contentId"], "action/1/1")

        self.assertIn("known server", self.run_cli_err("catalog", "avatar-library", "--server", "8", expect=4))
        self.assert_no_credential()

    def test_ticket_keys(self) -> None:
        rows = [json.loads(l) for l in self.run_cli("catalog", "ticket-keys", "-o", "jsonl").splitlines()]
        self.assertEqual([(r["kid"], r["revoked"]) for r in rows], [("k2", False), ("k1", True)])
        self.assertIn("BEGIN PUBLIC KEY", json.loads(self.run_cli("catalog", "ticket-keys", "-o", "json"))["keys"][0]["pem"])
        self.assert_no_credential()


class TestCreatorPublish(CliTestCase):
    GLB = b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 40

    def glb(self) -> str:
        path = os.path.join(self.config_dir, "cap.glb")
        with open(path, "wb") as handle:
            handle.write(self.GLB)
        return path

    def test_publish_sends_the_creator_token_and_the_raw_file(self) -> None:
        path = self.glb()
        with mock.patch.dict(os.environ, {"TMC_CREATOR_TOKEN": "tmcc_creator"}):
            out = json.loads(self.run_cli(
                "creator", "publish", path, "--kind", "wearable", "--name", "Red cap",
                "--slot", "hat", "--licence", "CC0-1.0", "--sharing", "public", "-o", "json",
            ))

        self.assertEqual((out["id"], out["held"]), (31, True))
        sent = STATE.published[-1]
        self.assertEqual((sent["bytes"], sent["type"]), (len(self.GLB), "model/gltf-binary"))
        self.assertEqual((sent["query"]["slot"], sent["query"]["licence"], sent["query"]["sharing"]), ("hat", "CC0-1.0", "PUBLIC"))
        # The content key on the line (`run_cli --token`) is never what goes out.
        self.assertEqual(STATE.auth_headers[-1], "Bearer tmcc_creator")

    def test_token_file_and_refusals(self) -> None:
        path = self.glb()
        token = os.path.join(self.config_dir, "creator.token")
        with open(token, "w", encoding="utf-8") as handle:
            handle.write("tmcc_creator\n")

        self.run_cli("creator", "publish", path, "--kind", "action", "--name", "Wave", "--loop", "--token-file", token)
        self.assertEqual(STATE.published[-1]["query"]["loop"], "1")

        with mock.patch.dict(os.environ, {"TMC_CREATOR_TOKEN": ""}):
            self.assertIn("creator token is required", self.run_cli_err("creator", "publish", path, "--kind", "avatar", "--name", "R", expect=2))
            # A wearable without a slot is refused before any upload is spent.
            self.assertIn("--slot and --licence", self.run_cli_err("creator", "publish", path, "--kind", "wearable", "--name", "C", expect=2))

        with open(path, "wb") as handle:
            handle.write(b"not a glb at all, really")
        self.assertIn("not a binary glTF", self.run_cli_err("creator", "publish", path, "--kind", "avatar", "--name", "R", "--token-file", token, expect=2))

        with mock.patch.dict(os.environ, {"TMC_CREATOR_TOKEN": "tmcc_wrong"}):
            self.assertIn("valid creator token", self.run_cli_err("creator", "publish", self.glb(), "--kind", "avatar", "--name", "R", expect=3))


class TestDefcon(CliTestCase):
    def test_status_url_moves_the_rest_reads(self) -> None:
        # website-status answers /api/status/defcon on its own host; the API
        # origin here is a dead port, so only --status-url can have served it.
        out = io.StringIO()

        with redirect_stdout(out):
            code = cli.main(
                ["--base-url", "http://127.0.0.1:9", "--status-url", self.server.base_url + "/",
                 "--retries", "0", "--yes", "defcon", "status", "-o", "jsonl"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["overall"], "DEGRADED")
        self.assertEqual(STATE.requests[-1][1], "/api/status/defcon")
        self.assertIsNone(STATE.auth_headers[-1])

    def test_summary_reads_the_status_site(self) -> None:
        row = json.loads(self.run_cli("defcon", "summary", "-o", "jsonl"))
        self.assertEqual((row["level"], row["open"], row["reporting"]), ("minor", 1, True))
        self.assertEqual(STATE.requests[-1][1], "/api/status/summary")
        self.assertIsNone(STATE.auth_headers[-1])

    def test_status_summary_and_check(self) -> None:
        row = json.loads(self.run_cli("defcon", "status", "-o", "jsonl"))
        self.assertEqual(
            (row["overall"], row["monitors"], row["degraded"], row["openIncidents"]),
            ("DEGRADED", 3, 1, 1),
        )
        self.assertEqual(STATE.requests[-1][1], "/api/status/defcon")
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
        self.assertTrue(sent.startswith("/api/status/defcon/series?"), sent)
        self.assertIn("range=week", sent)
        self.assertIn("monitorId=1", sent)

        summary = json.loads(self.run_cli("defcon", "latency", "1", "--summary", "-o", "json"))
        frankfurt = next(r for r in summary if r["node"] == "Frankfurt")
        self.assertEqual((frankfurt["latestMs"], frankfurt["meanMs"], frankfurt["peakMs"]), (44.0, 42.0, 90.0))

    def test_mtr(self) -> None:
        out = self.run_cli("defcon", "mtr", "Route", "-o", "csv")
        self.assertIn("10.0.0.1", out)

        err = self.run_cli_err("defcon", "mtr", "1", expect=0)
        self.assertIn("No traceroute", err)

    def test_incident_history_pages_through_all_of_it(self) -> None:
        rows = [json.loads(line) for line in self.run_cli("defcon", "incidents", "--all", "-o", "jsonl").splitlines()]
        self.assertEqual(len(rows), 130)
        self.assertEqual((rows[0]["id"], rows[-1]["id"]), (300, 171))
        self.assertIsNone(rows[0]["resolvedAt"])
        pages = [p for _, p in STATE.requests if p.startswith("/api/status/defcon/incidents")]
        self.assertEqual(len(pages), 2)
        self.assertIn("cursor=201", pages[1])

        STATE.defcon["show"] = {"nodes": True, "incidents": False, "mtr": True}
        self.assertIn("does not publish", self.run_cli_err("defcon", "incidents", "--all", expect=2))

    def test_incident_history_on_a_site_without_one(self) -> None:
        STATE.defcon_rest = False
        err = self.run_cli_err("defcon", "incidents", "--all", expect=2)
        self.assertIn("no public incident history", err)

    def test_a_site_without_the_rest_mirror_is_asked_over_trpc(self) -> None:
        STATE.defcon_rest = False
        row = json.loads(self.run_cli("defcon", "status", "-o", "jsonl"))
        self.assertEqual(row["overall"], "DEGRADED")
        paths = [p for _, p in STATE.requests]
        self.assertEqual(paths[0], "/api/status/defcon")
        self.assertTrue(paths[1].startswith("/api/trpc/defcon.public.status"))

        rows = self.run_cli("defcon", "latency", "1", "--range", "week", "-o", "csv")
        self.assertIn("defcon.public.series", STATE.requests[-1][1])
        self.assertGreater(len(rows.strip().splitlines()), 1)
        self.assertIn("10.0.0.1", self.run_cli("defcon", "mtr", "3", "-o", "csv"))

    def test_site_url_goes_straight_to_trpc(self) -> None:
        self.run_cli("defcon", "status", "--site-url", self.server.base_url)
        self.assertEqual(len(STATE.requests), 1)
        self.assertTrue(STATE.requests[0][1].startswith("/api/trpc/defcon.public.status"))

    def test_outages_and_one_outage(self) -> None:
        rows = [json.loads(l) for l in self.run_cli("defcon", "outages", "-o", "jsonl").splitlines()]
        self.assertEqual([r["id"] for r in rows], [40, 39, 38])
        self.assertEqual(rows[1]["monitors"], "Homepage, +1 private")
        self.assertTrue(STATE.requests[-1][1].startswith("/api/trpc/defcon.public.outages"))
        self.assertTrue(all(h is None for h in STATE.auth_headers))

        recent = [json.loads(l) for l in self.run_cli("defcon", "outages", "--since-hours", "24", "-o", "jsonl").splitlines()]
        self.assertEqual([r["id"] for r in recent], [40])
        self.assertIn("sinceHours", unquote(STATE.requests[-1][1]))

        paged = [json.loads(l) for l in self.run_cli("defcon", "outages", "--limit", "2", "--all", "-o", "jsonl").splitlines()]
        self.assertEqual(len(paged), 3)

        one = json.loads(self.run_cli("defcon", "outage", "40", "-o", "jsonl"))
        self.assertEqual((one["id"], one["notes"]), (40, 1))
        notes = json.loads(self.run_cli("defcon", "outage", "40", "--notes", "-o", "json"))
        self.assertEqual(notes[0]["body"], "redeploy")
        self.assertIn("No outage report 7", self.run_cli_err("defcon", "outage", "7", expect=2))

        STATE.defcon["enabled"] = False
        self.assertIn("does not publish outage", self.run_cli_err("defcon", "outages", expect=2))

    def test_dry_run_sends_nothing(self) -> None:
        self.run_cli("defcon", "monitors", "--dry-run")
        self.assertEqual(STATE.requests, [])


if __name__ == "__main__":
    unittest.main()
