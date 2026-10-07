"""The app-user session (`tmc auth app-login`) and the commands that run as it.

Over a real socket against `mock_server._app_user`, which answers the device
flow the way website-city does (202 while pending, PKCE S256, rotation with
reuse detection) and the per-user routes with the flat `{ok:false, code,
message}` envelope. The poll's sleep is patched out — and recorded, so the
backoff is asserted rather than waited for.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from mock_server import STATE  # noqa: E402
from test_cli import CliTestCase  # noqa: E402

from tmc_cli import cli  # noqa: E402
from tmc_cli.commands import appuser_cmd  # noqa: E402


class AppUserCase(CliTestCase):
    def setUp(self) -> None:
        super().setUp()

        # A fresh config per test: the session is what is under test.
        path = os.path.join(self.config_dir, "config.json")

        if os.path.exists(path):
            os.remove(path)

        self.waits: list[float] = []
        patcher = mock.patch.object(appuser_cmd, "_sleep", self.waits.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self) -> dict:
        with open(os.path.join(self.config_dir, "config.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def session(self) -> dict:
        return self.config()["profiles"]["default"]["app"]

    def run_both(self, *argv: str, expect: int = 0) -> tuple[str, str]:
        args = ["--base-url", self.server.base_url, "--yes", *argv]
        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(args)

        self.assertEqual(code, expect, f"tmc {' '.join(argv)}: {code}\n{out.getvalue()}\n{err.getvalue()}")

        return out.getvalue(), err.getvalue()

    def login(self) -> None:
        self.run_both("auth", "app-login")

    def j(self, *argv: str):
        return json.loads(self.run_cli(*argv, "-o", "json"))

    def rows(self, *argv: str) -> list:
        """The table's rows (flattened), rather than the API's own shape."""
        return [json.loads(line) for line in self.run_cli(*argv, "-o", "jsonl").splitlines()]


class TestDeviceLogin(AppUserCase):
    def test_pending_then_approved_stores_the_pair(self) -> None:
        out, err = self.run_both("auth", "app-login")

        self.assertIn("WDJB-MJHT", err)
        self.assertIn("login/device?code=WDJB-MJHT", err)
        self.assertEqual(STATE.app.polls, ["authorization_pending", "approved"])
        self.assertEqual(self.waits, [5.0, 5.0])

        session = self.session()
        self.assertTrue(session["access_token"].startswith("tmc_at_"))
        self.assertTrue(session["refresh_token"].startswith("tmc_rt_"))
        self.assertEqual(session["user"]["username"], "ann")
        self.assertEqual(session["base_url"], self.server.base_url)

        # Never printed, and the file is the owner's alone.
        self.assertNotIn(session["access_token"], out + err)
        self.assertNotIn(session["refresh_token"], out + err)
        mode = stat.S_IMODE(os.stat(os.path.join(self.config_dir, "config.json")).st_mode)
        self.assertEqual(mode, 0o600)

        # The device flow carries no bearer — not even the --token on the line.
        self.assertTrue(all(h is None for h in STATE.auth_headers))

        listed = json.loads(self.run_cli("auth", "list", "-o", "json"))
        self.assertEqual(listed[0]["appUser"], "ann")

    def test_slow_down_backs_off_for_good(self) -> None:
        STATE.app.plan = ["slow_down", "authorization_pending"]
        self.login()

        self.assertEqual(self.waits, [5.0, 10.0, 10.0])
        self.assertEqual(STATE.app.polls, ["slow_down", "authorization_pending", "approved"])

    def test_denied_stores_nothing(self) -> None:
        STATE.app.plan = ["access_denied"]
        _out, err = self.run_both("auth", "app-login", expect=1)

        self.assertIn("declined", err)
        self.assertFalse(os.path.exists(os.path.join(self.config_dir, "config.json")))

    def test_expiry_stops_polling(self) -> None:
        STATE.app.plan = ["authorization_pending"] * 500
        _out, err = self.run_both("auth", "app-login", expect=1)

        self.assertIn("expired", err)
        self.assertEqual(sum(self.waits), 600.0)

    def test_login_keeps_the_content_key(self) -> None:
        self.run_cli("auth", "login", "--no-verify")
        self.login()

        profile = self.config()["profiles"]["default"]
        self.assertTrue(profile["token"].startswith("tmc_"))
        self.assertIn("app", profile)


class TestSession(AppUserCase):
    def test_whoami_presents_the_app_token_not_the_content_key(self) -> None:
        self.login()
        row = json.loads(self.run_cli("auth", "app-whoami", "-o", "jsonl"))

        self.assertEqual((row["id"], row["username"]), ("u1", "ann"))
        self.assertTrue(STATE.auth_headers[-1].startswith("Bearer tmc_at_"))

    def test_a_401_is_refreshed_once_and_the_new_pair_saved(self) -> None:
        self.login()
        before = self.session()
        STATE.app.expire_all_access()

        rows = self.j("friends", "list")

        self.assertEqual(rows[0]["userId"], "u2")
        self.assertEqual(STATE.app.refreshes, 1)

        after = self.session()
        self.assertNotEqual(after["access_token"], before["access_token"])
        self.assertNotEqual(after["refresh_token"], before["refresh_token"])

        # The rotated pair works on the next call with no further refresh.
        self.j("friends", "list")
        self.assertEqual(STATE.app.refreshes, 1)

    def test_a_stale_expiry_refreshes_before_sending(self) -> None:
        self.login()
        path = os.path.join(self.config_dir, "config.json")
        doc = self.config()
        doc["profiles"]["default"]["app"]["expires_at"] = 0

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)

        sent_before = len(STATE.requests)
        self.j("friends", "list")

        paths = [p for _, p in STATE.requests[sent_before:]]
        self.assertEqual(paths, ["/api/app/v1/auth/refresh", "/api/app/v1/friends"])

    def test_a_dead_refresh_token_fails_clearly_and_forgets(self) -> None:
        self.login()
        STATE.app.expire_all_access()
        STATE.app.refresh.clear()

        err = self.run_cli_err("friends", "list", expect=1)

        self.assertIn("could not be renewed", err)
        self.assertIn("app-login", err)
        self.assertEqual(STATE.app.refreshes, 1)
        self.assertNotIn("app", self.config()["profiles"]["default"])

    def test_still_401_after_refresh_is_not_retried_again(self) -> None:
        self.login()
        STATE.app.expire_all_access()

        original = STATE.app.issue

        def issue_dead(user_id: str) -> dict:
            pair = original(user_id)
            STATE.app.access.pop(pair["accessToken"])
            return pair

        STATE.app.issue = issue_dead  # type: ignore[method-assign]
        err = self.run_cli_err("friends", "list", expect=3)

        self.assertIn("token_expired", err)
        self.assertEqual(STATE.app.refreshes, 1)

    def test_no_session(self) -> None:
        err = self.run_cli_err("friends", "list", expect=2)
        self.assertIn("app-login", err)

    def test_token_is_bound_to_its_site(self) -> None:
        self.login()
        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--base-url", "http://127.0.0.1:9", "friends", "list"])

        self.assertEqual(code, 2)
        self.assertIn("signed in at", err.getvalue())

    def test_env_token(self) -> None:
        STATE.app.access["tmc_at_env"] = "u1"

        with mock.patch.dict(os.environ, {"TMC_APP_TOKEN": "tmc_at_env"}):
            rows = self.j("friends", "list")

        self.assertEqual(rows[0]["displayName"], "Bob")
        self.assertEqual(STATE.auth_headers[-1], "Bearer tmc_at_env")

    def test_logout_revokes_and_forgets(self) -> None:
        self.login()
        self.run_cli("auth", "app-logout")

        self.assertEqual(STATE.app.revoked, 1)
        self.assertNotIn("app", self.config()["profiles"]["default"])
        self.run_cli_err("friends", "list", expect=2)

    def test_catalog_still_sends_nothing(self) -> None:
        self.login()
        self.run_cli("catalog", "games", "-o", "json")
        self.assertIsNone(STATE.auth_headers[-1])


class TestGroups(AppUserCase):
    def setUp(self) -> None:
        super().setUp()
        self.login()

    def test_friends(self) -> None:
        reqs = self.rows("friends", "requests")
        self.assertEqual(reqs[0], {"direction": "incoming", "id": 11, "userId": "u3", "displayName": "Cat", "createdAt": "2026-10-01T10:00:00Z"})

        self.run_cli("friends", "accept", "11")
        self.assertEqual([f["userId"] for f in self.j("friends", "list")], ["u2", "u3"])

        sent = self.j("friends", "add", "u2")
        self.assertIsInstance(sent["id"], int)
        self.run_cli("friends", "cancel", str(sent["id"]))
        self.run_cli("friends", "remove", "u3")
        self.assertEqual([f["userId"] for f in self.j("friends", "list")], ["u2"])

        err = self.run_cli_err("friends", "accept", "999", expect=4)
        self.assertIn("friends.respond.deny.notFound", err)
        self.assertIn("could not be found", err)

    def test_presence(self) -> None:
        rows = self.rows("presence", "get", "u2", "u3")
        self.assertEqual([(r["userId"], r["status"], r["app"]) for r in rows], [("u2", "in_game", "Seed")])
        self.assertIn("userIds=u2%2Cu3", STATE.requests[-1][1])

        self.assertEqual(self.j("presence", "set", "online", "--detail", "lobby"), {"ttlSec": 120})
        self.assertEqual(STATE.app.presence["u1"]["detail"], "lobby")

        err = self.run_cli_err("presence", "set", "online", "--party", "abc", expect=5)
        self.assertIn("bad_request", err)

    def test_party(self) -> None:
        self.assertEqual(self.run_cli("party", "mine", "-o", "json").strip(), "null")

        party = self.j("party", "create", "--app", "1", "--name", "Ann's", "--max-users", "4")
        pid = party["id"]
        self.assertEqual((party["hostId"], party["users"], party["maxUsers"]), ("u1", 1, 4))

        invited = self.j("party", "invite", pid, "u2", "u3", "--message", "hi")
        self.assertEqual(len(invited), 2)

        self.assertEqual(self.j("party", "mine")["id"], pid)
        members = self.run_cli("party", "show", pid, "--members", "-o", "csv")
        self.assertIn("u1,Ann,HOST", members)

        ready = json.loads(self.run_cli("party", "ready", pid, "-o", "jsonl"))
        self.assertEqual((ready["ready"], ready["total"], ready["meReady"]), (1, 1, True))

        err = self.run_cli_err("party", "start", pid, expect=5)
        self.assertIn("Pick a server first.", err)
        started = json.loads(self.run_cli("party", "start", pid, "--server", "7", "-o", "jsonl"))
        self.assertTrue(started["started"])

        err = self.run_cli_err("party", "kick", pid, "u3", expect=4)
        self.assertIn("party.kick.deny.notMember", err)

        self.assertEqual(self.j("party", "leave", pid, "--silent"), {"left": True, "ended": True})

    def test_party_invites_and_join(self) -> None:
        invites = self.j("party", "invites")
        self.assertEqual(invites[0]["id"], "900")

        joined = self.j("party", "accept", "900")
        self.assertEqual(joined, {"partyId": "500", "role": "MEMBER", "leftPartyId": None})

        own = self.j("party", "create", "--app", "1", "--type", "PUBLIC_PASSWORD", "--password", "secret1")
        err = self.run_cli_err("party", "join", own["id"], "--password", "nope", expect=3)
        self.assertIn("Wrong password.", err)

    def test_installs(self) -> None:
        made = self.j("installs", "create", "--app", "1", "--name", "Main", "--loader", "fabric")
        iid = str(made["id"])
        self.assertEqual((made["isDefault"], made["loader"], made["strategy"]), (True, "fabric", "hardlink"))

        rows = self.j("installs", "list", "--app", "1")
        self.assertEqual([r["name"] for r in rows], ["Main"])

        updated = self.j("installs", "update", iid, "--name", "Renamed", "--environment", "server")
        self.assertEqual((updated["name"], updated["environment"]), ("Renamed", "server"))

        items = self.j("installs", "items", "add", iid, "mod", "5")
        self.assertEqual(items["items"][0]["itemId"], 5)
        table = self.run_cli("installs", "items", "set", iid, "mod", "5", "--disable", "-o", "csv")
        self.assertIn("mod,5,mod 5,False,0", table)
        self.assertEqual(self.j("installs", "items", "remove", iid, "mod", "5")["items"], [])

        self.run_cli("installs", "delete", iid)
        self.assertEqual(self.j("installs", "list"), [])

        err = self.run_cli_err("installs", "create", "--app", "2", "--name", "X", expect=5)
        self.assertIn("app_unsupported", err)

    def test_subscriptions(self) -> None:
        self.assertEqual(self.j("subscriptions", "add", "mod", "5"), {"subscribed": True, "propagated": 0, "skipped": 0})

        rows = self.j("subscriptions", "list")
        self.assertEqual(rows["items"][0]["itemId"], 5)
        self.assertIn("mod,5,mod 5,Seed,1.0,True", self.run_cli("subscriptions", "list", "-o", "csv"))

        self.run_cli("subscriptions", "prefs", "mod", "5", "--no-auto-update", "--paused")
        self.assertEqual(STATE.app.subs[("mod", 5)]["autoUpdate"], False)
        self.assertEqual(STATE.app.subs[("mod", 5)]["paused"], True)

        self.assertFalse(self.j("subscriptions", "remove", "mod", "5")["subscribed"])

        err = self.run_cli_err("subscriptions", "prefs", "mod", "5", "--paused", expect=4)
        self.assertIn("You are not subscribed", err)

    def test_reviews(self) -> None:
        self.assertEqual(self.j("reviews", "write", "mod", "5", "--rating", "4"), {"saved": True, "edited": False, "held": False})
        self.assertTrue(self.j("reviews", "write", "mod", "5", "--content", "better now")["edited"])

        own = STATE.app.reviews[("mod", 5)]["id"]
        err = self.run_cli_err("reviews", "vote", str(own), "up", expect=5)
        self.assertIn("own_review", err)
        self.assertEqual(self.j("reviews", "vote", "77", "clear"), {"voted": None})

        self.run_cli("reviews", "delete", str(own))
        self.assertEqual(STATE.app.reviews, {})

        self.run_cli_err("reviews", "write", "mod", "5", expect=2)

    def test_stats(self) -> None:
        rows = self.j("stats", "top", "1", "kills", "--limit", "2")
        self.assertEqual(rows["rows"][0], {"rank": 1, "player": "p-u2", "name": "Bob", "value": 50.0})
        self.assertEqual(rows["self"]["rank"], 2)
        self.assertIn("app=1&stat=kills&limit=2", STATE.requests[-1][1])

        _out, err = self.run_both("stats", "top", "1", "kills")
        self.assertIn("you: #2", err)

        mine = self.j("stats", "me", "1")
        self.assertEqual(mine["stats"][0]["value"], 30.0)
        self.assertEqual(self.j("stats", "defs", "1")[0]["key"], "kills")

        err = self.run_cli_err("stats", "top", "1", "deaths", expect=4)
        self.assertIn("No such stat.", err)


if __name__ == "__main__":
    unittest.main()
