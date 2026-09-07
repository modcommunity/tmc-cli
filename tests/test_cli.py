"""End-to-end tests: the real CLI against the mock API, over a real socket.

Every test drives `cli.main(argv)` exactly as a shell would, so what is being
tested is the whole path — argument parsing, payload coercion, the transport's
retries, the batching in `client.py` — and not a set of functions called in a
convenient order.
"""

from __future__ import annotations

import io
import json
import os
import sys
import shutil
import stat
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mock_server import BEARER_TOKEN, STATE, MockServer  # noqa: E402

from tmc_cli import cli  # noqa: E402
from tmc_cli.ed25519 import SigningKey, _verify_pure, generate_seed, pem_to_seed  # noqa: E402


class CliTestCase(unittest.TestCase):
    """Boots the mock, points the config at a temp dir, runs commands."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = MockServer().__enter__()
        cls.config_dir = tempfile.mkdtemp(prefix="tmc-test-")

        os.environ["TMC_CONFIG_DIR"] = cls.config_dir
        os.environ["NO_COLOR"] = "1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.__exit__(None, None, None)

    def setUp(self) -> None:
        STATE.reset()

    def run_cli(self, *argv: str, expect: int = 0) -> str:
        """Run one command; return stdout. Fails the test on an unexpected code."""

        args = ["--base-url", self.server.base_url, "--token", BEARER_TOKEN, "--yes", *argv]

        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(args)

        self.assertEqual(
            code,
            expect,
            f"tmc {' '.join(argv)} exited {code}, expected {expect}\n"
            f"stdout: {out.getvalue()}\nstderr: {err.getvalue()}",
        )

        return out.getvalue()

    def run_cli_err(self, *argv: str, expect: int) -> str:
        """Same, but returns stderr — where errors and progress are written."""

        args = ["--base-url", self.server.base_url, "--token", BEARER_TOKEN, "--yes", *argv]

        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(args)

        self.assertEqual(code, expect, f"expected exit {expect}, got {code}: {err.getvalue()}")

        return err.getvalue()

    def run_anon(self, *argv: str, expect: int = 0) -> tuple[str, str]:
        """Run with `--anon` and NO token — the credential must be absent.

        Deliberately not `run_cli(..., "--anon")`: that would still put
        `--token` on the line, and the whole question being tested is whether
        the transport sends a header at all.
        """

        args = ["--base-url", self.server.base_url, "--anon", "--yes", *argv]

        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(args)

        self.assertEqual(
            code,
            expect,
            f"tmc {' '.join(argv)} exited {code}, expected {expect}\n"
            f"stdout: {out.getvalue()}\nstderr: {err.getvalue()}",
        )

        return out.getvalue(), err.getvalue()

    def seed_mod(self, **fields) -> int:
        row = {"id": STATE.take_id(), "name": "Seed Mod", "appId": 1, "hidden": False, **fields}
        STATE.rows["mod"][row["id"]] = row

        return row["id"]


# ---- crypto -----------------------------------------------------------------


class TestEd25519(unittest.TestCase):
    def test_rfc8032_vector(self) -> None:
        key = SigningKey(
            bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
        )

        self.assertEqual(
            key.public_bytes().hex(),
            "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        )
        self.assertEqual(
            key.sign(bytes.fromhex("72")).hex(),
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
        )

    def test_self_test_and_tamper(self) -> None:
        key = SigningKey(generate_seed())

        self.assertTrue(key.self_test())

        signature = key.sign(b"hello")

        self.assertTrue(_verify_pure(key.public_bytes(), b"hello", signature))
        self.assertFalse(_verify_pure(key.public_bytes(), b"hello!", signature))

    def test_pem_round_trip(self) -> None:
        """A real `openssl genpkey -algorithm ed25519` PEM, pinned.

        Both expectations came from openssl itself — the seed from the DER
        below, the public key from `openssl pkey -pubout` — so this checks the
        DER walk and the scalar derivation against an outside implementation
        rather than against ourselves.
        """

        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MC4CAQAwBQYDK2VwBCIEIP4N2nSBvS7EDTQx+uzSjxtRNGhRCM/KOn60gs4NwQ2h\n"
            "-----END PRIVATE KEY-----\n"
        )

        self.assertEqual(
            pem_to_seed(pem).hex(),
            "fe0dda7481bd2ec40d3431faecd28f1b5134685108cfca3a7eb482ce0dc10da1",
        )
        self.assertEqual(
            SigningKey.from_pem(pem).public_bytes().hex(),
            "d1de1eafc4ea2d532229784cfb9afa7f8d669f4c0ee5cf493e625502518bb209",
        )

    def test_rejects_encrypted_pem(self) -> None:
        from tmc_cli.ed25519 import KeyError_

        with self.assertRaises(KeyError_) as caught:
            pem_to_seed(
                "-----BEGIN ENCRYPTED PRIVATE KEY-----\nQUJD\n-----END ENCRYPTED PRIVATE KEY-----\n"
            )

        self.assertIn("Decrypt it first", str(caught.exception))

    def test_rejects_non_ed25519_pem(self) -> None:
        from tmc_cli.ed25519 import KeyError_

        with self.assertRaises(KeyError_):
            pem_to_seed("-----BEGIN PRIVATE KEY-----\nQUJD\n-----END PRIVATE KEY-----\n")


# ---- content CRUD -----------------------------------------------------------


class TestContent(CliTestCase):
    def test_create_coerces_types(self) -> None:
        self.run_cli(
            "mod", "create",
            "--set", "name=My Mod",
            "--set", "content=Body",
            "--set", "appId=1",
            "--set", "hidden=true",
        )

        row = next(iter(STATE.rows["mod"].values()))

        # The API's schemas do not coerce: "1" and "true" would both be 400s.
        self.assertEqual(row["appId"], 1)
        self.assertIs(row["hidden"], True)
        self.assertIsInstance(row["appId"], int)

    def test_create_rejects_unknown_field_locally(self) -> None:
        before = len(STATE.requests)

        stderr = self.run_cli_err(
            "mod", "create", "--set", "name=x", "--set", "content=y",
            "--set", "appId=1", "--set", "tgs=pvp",
            expect=2,
        )

        self.assertIn("has no field 'tgs'", stderr)
        self.assertIn("Did you mean 'tags'?", stderr)
        # Caught before the request — that is the point of the local mirror.
        self.assertEqual(len(STATE.requests), before)

    def test_create_requires_declared_fields(self) -> None:
        stderr = self.run_cli_err("mod", "create", "--set", "name=x", expect=2)

        self.assertIn("missing content, appId", stderr)

    def test_server_validation_failure_shows_the_failing_fields(self) -> None:
        """With the local check waived, the server's zod issues reach the user."""

        stderr = self.run_cli_err(
            "mod", "create", "--allow-unknown-fields", "--json", '{"name": "x"}',
            expect=5,
        )

        self.assertIn("Validation failed", stderr)
        # zod's flatten() output is rendered as `field: message`, not swallowed.
        self.assertIn("content: Required", stderr)
        self.assertIn("appId: Required", stderr)

    def test_json_body_is_the_base_and_set_wins(self) -> None:
        self.run_cli(
            "mod", "create",
            "--json", '{"name": "From JSON", "content": "Body", "appId": 1}',
            "--set", "name=Overridden",
        )

        row = next(iter(STATE.rows["mod"].values()))

        self.assertEqual(row["name"], "Overridden")
        self.assertEqual(row["content"], "Body")

    def test_list_pagination_all(self) -> None:
        for index in range(30):
            self.seed_mod(name=f"Mod {index}")

        output = self.run_cli("mod", "list", "--limit", "10", "--all", "-o", "ids")

        self.assertEqual(len(output.strip().splitlines()), 30)

    def test_list_max_caps_results(self) -> None:
        for index in range(30):
            self.seed_mod(name=f"Mod {index}")

        output = self.run_cli("mod", "list", "--limit", "10", "--all", "--max", "12", "-o", "ids")

        self.assertEqual(len(output.strip().splitlines()), 12)

    def test_list_hides_hidden_unless_mine(self) -> None:
        self.seed_mod(name="Public")
        self.seed_mod(name="Draft", hidden=True)

        self.assertEqual(len(self.run_cli("mod", "list", "-o", "ids").strip().splitlines()), 1)
        self.assertEqual(
            len(self.run_cli("mod", "list", "--mine", "-o", "ids").strip().splitlines()), 2
        )

    def test_update_is_partial(self) -> None:
        mod_id = self.seed_mod(name="Before", content="Keep me")

        self.run_cli("mod", "update", str(mod_id), "--set", "name=After")

        row = STATE.rows["mod"][mod_id]

        self.assertEqual(row["name"], "After")
        self.assertEqual(row["content"], "Keep me")

    def test_set_file_reads_body_from_disk(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("# Release notes\n\nLots of them.")
            path = handle.name

        self.run_cli(
            "mod", "create",
            "--set", "name=Doc Mod", "--set", "appId=1",
            "--content-file", path,
        )

        row = next(iter(STATE.rows["mod"].values()))

        self.assertIn("# Release notes", row["content"])

    def test_bulk_create_batches_past_the_cap(self) -> None:
        items = [{"name": f"Bulk {i}", "content": "x", "appId": 1} for i in range(60)]

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(items, handle)
            path = handle.name

        self.run_cli("mod", "create", "--from-file", path, "-o", "ids")

        self.assertEqual(len(STATE.rows["mod"]), 60)

        # 25 + 25 + 10: the server refuses anything larger in one request.
        posts = [r for r in STATE.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 3)

    def test_bulk_delete_batches(self) -> None:
        ids = [self.seed_mod(name=f"Doomed {i}") for i in range(150)]

        self.run_cli("mod", "delete", *[str(i) for i in ids])

        self.assertEqual(len(STATE.rows["mod"]), 0)
        self.assertEqual(len([r for r in STATE.requests if r[0] == "DELETE"]), 2)

    def test_delete_reports_ids_that_were_already_gone(self) -> None:
        first = self.seed_mod(name="Real")

        stderr = self.run_cli_err("mod", "delete", str(first), "9999", expect=0)

        self.assertIn("9999", stderr)

    def test_output_formats(self) -> None:
        self.seed_mod(name="Formatted")

        as_json = json.loads(self.run_cli("mod", "list", "-o", "json"))
        self.assertEqual(as_json[0]["name"], "Formatted")

        self.assertIn("name", self.run_cli("mod", "list", "-o", "csv").splitlines()[0])
        self.assertTrue(self.run_cli("mod", "list", "-o", "ids").strip().isdigit())

    def test_field_projection(self) -> None:
        self.seed_mod(name="Slim")

        rows = json.loads(self.run_cli("mod", "list", "-o", "json", "--field", "id,name"))

        self.assertEqual(set(rows[0]), {"id", "name"})

    def test_dry_run_sends_nothing(self) -> None:
        self.run_cli("--dry-run", "mod", "create", "--set", "name=Ghost", "--set", "content=x", "--set", "appId=1")

        self.assertEqual(STATE.rows["mod"], {})


# ---- relations --------------------------------------------------------------


class TestRelations(CliTestCase):
    def test_tags_add_is_additive(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli("tags", "add", "mod", str(mod_id), "pvp")
        self.run_cli("tags", "add", "mod", str(mod_id), "vanilla")

        self.assertEqual(STATE.relations[("mod", mod_id, "tags")], ["pvp", "vanilla"])

    def test_tags_set_replaces(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli("tags", "add", "mod", str(mod_id), "pvp", "vanilla")
        self.run_cli("tags", "set", "mod", str(mod_id), "only-this")

        self.assertEqual(STATE.relations[("mod", mod_id, "tags")], ["only-this"])

    def test_tags_rm_folds_case(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli("tags", "add", "mod", str(mod_id), "PvP", "vanilla")
        self.run_cli("tags", "rm", "mod", str(mod_id), "pvp")

        self.assertEqual(STATE.relations[("mod", mod_id, "tags")], ["vanilla"])

    def test_rel_clear(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli("tags", "add", "mod", str(mod_id), "a", "b")
        self.run_cli("rel", "clear", "mod", str(mod_id), "tags")

        self.assertEqual(STATE.relations[("mod", mod_id, "tags")], [])

    def test_relation_not_available_on_type(self) -> None:
        stderr = self.run_cli_err("rel", "get", "collection", "1", "links", expect=2)

        self.assertIn("no 'links' relation", stderr)

    def test_media_add_uploads_and_attaches(self) -> None:
        mod_id = self.seed_mod()

        with tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False) as handle:
            handle.write(b"\x89PNG fake bytes")
            path = handle.name

        self.run_cli("media", "add", "mod", str(mod_id), "--file", path, "--title", "Shot")

        members = STATE.relations[("mod", mod_id, "media")]

        self.assertEqual(len(members), 1)
        # The file was uploaded first and the resulting id attached — one command.
        self.assertIn(members[0]["fileId"], STATE.files)
        self.assertEqual(members[0]["title"], "Shot")

    def test_media_add_external_url(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli(
            "media", "add", "mod", str(mod_id),
            "--url", "https://example.com/a.png", "--type", "IMAGE",
        )

        members = STATE.relations[("mod", mod_id, "media")]

        self.assertEqual(members[0]["externalUrl"], "https://example.com/a.png")
        self.assertEqual(members[0]["type"], "IMAGE")

    def test_links_add_with_type(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli(
            "links", "add", "mod", str(mod_id),
            "https://github.com/me/repo", "--type", "GITHUB",
        )

        self.assertEqual(STATE.relations[("mod", mod_id, "links")][0]["type"], "GITHUB")

    def test_rel_add_json_member(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli(
            "rel", "add", "mod", str(mod_id), "sources",
            "--member", '{"sourceId": 3, "path": "/mods/mine"}',
        )

        self.assertEqual(STATE.relations[("mod", mod_id, "sources")][0]["sourceId"], 3)

    def test_rel_add_rejects_bare_word_for_object_relation(self) -> None:
        mod_id = self.seed_mod()

        stderr = self.run_cli_err("rel", "add", "mod", str(mod_id), "media", "oops", expect=2)

        self.assertIn("not a valid 'media' member", stderr)


# ---- files ------------------------------------------------------------------


class TestFiles(CliTestCase):
    def _make_files(self, count: int, suffix: str = ".zip") -> list[str]:
        paths = []

        for index in range(count):
            handle = tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False)
            handle.write(b"payload-%d" % index)
            handle.close()
            paths.append(handle.name)

        return paths

    def test_upload_batches_past_twenty(self) -> None:
        paths = self._make_files(25)

        self.run_cli("file", "upload", *paths)

        self.assertEqual(len(STATE.files), 25)
        # 20 is the server's per-request part cap.
        self.assertEqual(len([r for r in STATE.requests if r[0] == "POST"]), 2)

    def test_upload_glob_pattern(self) -> None:
        directory = tempfile.mkdtemp()

        for index in range(3):
            with open(os.path.join(directory, f"file{index}.zip"), "wb") as handle:
                handle.write(b"bytes")

        self.run_cli("file", "upload", os.path.join(directory, "*.zip"))

        self.assertEqual(len(STATE.files), 3)

    def test_upload_refuses_empty_file(self) -> None:
        handle = tempfile.NamedTemporaryFile("wb", suffix=".zip", delete=False)
        handle.close()

        stderr = self.run_cli_err("file", "upload", handle.name, expect=2)

        self.assertIn("is empty", stderr)

    def test_upload_raw_form(self) -> None:
        path = self._make_files(1)[0]

        self.run_cli("file", "upload", path, "--raw", "--name", "mod.zip")

        row = next(iter(STATE.files.values()))

        self.assertEqual(row["title"], "mod.zip")

    def test_file_update_and_delete(self) -> None:
        path = self._make_files(1)[0]

        self.run_cli("file", "upload", path)
        file_id = next(iter(STATE.files))

        self.run_cli("file", "update", file_id, "--title", "Renamed")
        self.assertEqual(STATE.files[file_id]["title"], "Renamed")

        self.run_cli("file", "rm", file_id)
        self.assertEqual(STATE.files, {})


# ---- releases ---------------------------------------------------------------


class TestReleases(CliTestCase):
    def _file(self, name: str) -> str:
        handle = tempfile.NamedTemporaryFile("wb", suffix=f"-{name}", delete=False)
        handle.write(b"a release artifact")
        handle.close()

        return handle.name

    def test_publish_creates_with_files(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli(
            "release", "publish", "--mod", str(mod_id),
            "--version", "1.0.0", "--title", "First",
            "--file", self._file("a.zip"),
        )

        releases = STATE.relations[("mod", mod_id, "releases")]

        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["version"], "1.0.0")
        self.assertEqual(len(releases[0]["files"]), 1)
        self.assertIn(releases[0]["files"][0], STATE.files)

    def test_publish_merges_files_by_default(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli(
            "release", "publish", "--mod", str(mod_id),
            "--version", "1.0.0", "--file", self._file("a.zip"),
        )
        self.run_cli(
            "release", "publish", "--mod", str(mod_id),
            "--version", "1.0.0", "--file", self._file("b.zip"),
        )

        releases = STATE.relations[("mod", mod_id, "releases")]

        self.assertEqual(len(releases), 1, "a second publish of the same version must update, not append")
        self.assertEqual(len(releases[0]["files"]), 2)

    def test_publish_replace_files(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli("release", "publish", "--mod", str(mod_id), "--version", "1.0.0", "--file", self._file("a.zip"))
        self.run_cli(
            "release", "publish", "--mod", str(mod_id), "--version", "1.0.0",
            "--file", self._file("b.zip"), "--replace-files",
        )

        releases = STATE.relations[("mod", mod_id, "releases")]

        self.assertEqual(len(releases[0]["files"]), 1)

    def test_publish_preserves_hidden(self) -> None:
        """The server resets `hidden` when a release update omits it."""

        mod_id = self.seed_mod()

        self.run_cli(
            "release", "publish", "--mod", str(mod_id), "--version", "1.0.0", "--hidden",
        )
        self.assertTrue(STATE.relations[("mod", mod_id, "releases")][0]["hidden"])

        # A later publish that says nothing about visibility must not un-hide it.
        self.run_cli(
            "release", "publish", "--mod", str(mod_id), "--version", "1.0.0", "--title", "Renamed",
        )

        release = STATE.relations[("mod", mod_id, "releases")][0]

        self.assertTrue(release["hidden"], "publishing again must not un-hide a draft release")
        self.assertEqual(release["title"], "Renamed")

    def test_publish_leaves_other_releases_alone(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli("release", "publish", "--mod", str(mod_id), "--version", "1.0.0")
        self.run_cli("release", "publish", "--mod", str(mod_id), "--version", "1.1.0")

        versions = [r["version"] for r in STATE.relations[("mod", mod_id, "releases")]]

        self.assertEqual(versions, ["1.0.0", "1.1.0"])

    def test_publish_requires_exactly_one_parent(self) -> None:
        stderr = self.run_cli_err("release", "publish", "--version", "1.0.0", expect=2)
        self.assertIn("Name the item", stderr)

        stderr = self.run_cli_err(
            "release", "publish", "--mod", "1", "--asset", "2", "--version", "1.0.0", expect=2
        )
        self.assertIn("exactly one item", stderr)

    def test_release_rm(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli("release", "publish", "--mod", str(mod_id), "--version", "1.0.0")
        self.run_cli("release", "publish", "--mod", str(mod_id), "--version", "2.0.0")
        self.run_cli("release", "rm", "--mod", str(mod_id), "--version", "1.0.0")

        versions = [r["version"] for r in STATE.relations[("mod", mod_id, "releases")]]

        self.assertEqual(versions, ["2.0.0"])

    def test_release_files_resolves_metadata(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli(
            "release", "publish", "--mod", str(mod_id), "--version", "1.0.0",
            "--file", self._file("a.zip"),
        )

        rows = json.loads(
            self.run_cli("release", "files", "--mod", str(mod_id), "--version", "1.0.0", "-o", "json")
        )

        self.assertEqual(len(rows), 1)
        self.assertIn("cdn.test", rows[0]["url"])


# ---- auth -------------------------------------------------------------------


class TestAuth(CliTestCase):
    def test_bearer_rejected_when_wrong(self) -> None:
        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--base-url", self.server.base_url, "--token", "tmc_wrong", "mod", "list"]
            )

        self.assertEqual(code, 3)
        self.assertIn("Invalid API token", err.getvalue())

    def test_jwt_signs_a_fresh_assertion_per_request(self) -> None:
        seed = generate_seed()
        key = SigningKey(seed)
        key_id = "tmcak_" + "ab" * 12

        STATE.public_keys[key_id] = key.public_bytes()

        pem_path = os.path.join(self.config_dir, "jwt-test.pem")

        with open(pem_path, "w", encoding="utf-8") as handle:
            handle.write(_pkcs8_pem(seed))

        args = [
            "--base-url", self.server.base_url,
            "--key-id", key_id, "--private-key", pem_path,
        ]

        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            # Two requests in a row: the mock consumes each jti, so a reused
            # header would be a 400 on the second.
            first = cli.main([*args, "mod", "list"])
            second = cli.main([*args, "mod", "list"])

        self.assertEqual((first, second), (0, 0), err.getvalue())
        self.assertEqual(len(STATE.seen_jti), 2)

    def test_jwt_replay_is_refused_by_the_server(self) -> None:
        """Proves the mock enforces what makes per-attempt signing necessary."""

        seed = generate_seed()
        key = SigningKey(seed)
        key_id = "tmcak_" + "cd" * 12

        STATE.public_keys[key_id] = key.public_bytes()

        from tmc_cli.auth import JwtCredential
        from tmc_cli.http import Transport

        credential = JwtCredential(key_id, key)
        assertion = credential.assertion()

        transport = Transport(self.server.base_url, credential, retries=0)

        # Send the same assertion twice by hand.
        import urllib.error
        import urllib.request

        def send() -> int:
            request = urllib.request.Request(
                f"{self.server.base_url}/api/content/mod",
                headers={"Authorization": f"Bearer {assertion}"},
            )

            try:
                with urllib.request.urlopen(request) as response:
                    return response.status
            except urllib.error.HTTPError as err:
                return err.code

        self.assertEqual(send(), 200)
        self.assertEqual(send(), 400)

        del transport

    def test_login_stores_profile_and_whoami_reports_permissions(self) -> None:
        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                [
                    "auth", "login",
                    "--profile", "testing",
                    "--base-url", self.server.base_url,
                    "--token", BEARER_TOKEN,
                    "--set-default",
                ]
            )

        self.assertEqual(code, 0, err.getvalue())

        config_file = os.path.join(self.config_dir, "config.json")
        self.assertTrue(os.path.exists(config_file))
        self.assertEqual(os.stat(config_file).st_mode & 0o777, 0o600)

        # The stored profile alone is enough — no flags on the next call.
        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["auth", "whoami", "-o", "json"])

        self.assertEqual(code, 0, err.getvalue())

        facts = json.loads(out.getvalue())

        self.assertIs(facts["canRead"], True)
        self.assertIs(facts["canWrite"], True)
        self.assertIs(facts["canDelete"], True)
        self.assertEqual(facts["authMode"], "bearer")

    def test_whoami_detects_a_read_only_key(self) -> None:
        STATE.can_write = False
        STATE.can_delete = False

        try:
            facts = json.loads(self.run_cli("auth", "whoami", "-o", "json"))
        finally:
            STATE.can_write = True
            STATE.can_delete = True

        self.assertIs(facts["canRead"], True)
        self.assertIs(facts["canWrite"], False)
        self.assertIs(facts["canDelete"], False)

    def test_whoami_write_probe_creates_nothing(self) -> None:
        self.run_cli("auth", "whoami", "-o", "json")

        self.assertEqual(STATE.rows["asset"], {})


# ---- transport --------------------------------------------------------------


class TestSchemaMirror(CliTestCase):
    """The mirror, where it disagreeing with the server costs a round trip."""

    def test_api_public_is_settable(self) -> None:
        mod_id = self.seed_mod()

        self.run_cli("mod", "update", str(mod_id), "--set", "apiPublic=false")

        self.assertIs(STATE.rows["mod"][mod_id]["apiPublic"], False)

    def test_servers_have_no_api_public(self) -> None:
        """The one type `content.ts` leaves it off — it picks columns explicitly."""

        err = self.run_cli_err(
            "server", "update", "1", "--set", "apiPublic=false", expect=2
        )

        self.assertIn("has no field 'apiPublic'", err)

    def test_new_server_fields_are_known(self) -> None:
        row = {"id": STATE.take_id(), "appId": 1, "hidden": False}
        STATE.rows["server"][row["id"]] = row

        self.run_cli(
            "server",
            "update",
            str(row["id"]),
            "--set", "countryId=42",
            "--set", "useHostName=true",
            "--set", "showSlideshow=false",
            "--set", "archived=true",
            "--set", "allowMedia=true",
            "--set", "categoryIds=3,7",
        )

        stored = STATE.rows["server"][row["id"]]

        # Coerced, not passed through as strings — the schemas do not coerce.
        self.assertEqual(stored["countryId"], 42)
        self.assertIs(stored["useHostName"], True)
        self.assertEqual(stored["categoryIds"], [3, 7])

    def test_groups_have_a_tags_relation(self) -> None:
        """`relations.ts` lists group under tags, and under nothing else."""

        row = {"id": STATE.take_id(), "name": "A Group", "hidden": False}
        STATE.rows["group"][row["id"]] = row

        self.run_cli("tags", "add", "group", str(row["id"]), "modding", "tools")

        out = self.run_cli("tags", "list", "group", str(row["id"]), "-o", "json")

        self.assertEqual(sorted(json.loads(out)), ["modding", "tools"])

    def test_group_has_no_media_relation(self) -> None:
        err = self.run_cli_err("rel", "get", "group", "1", "media", expect=2)

        self.assertIn("no 'media' relation", err)

    def test_relation_delete_batches_at_the_key_cap(self) -> None:
        """DELETE takes keys, which meet the 500 body cap, not the 200 one."""

        mod_id = self.seed_mod()

        names = [f"tag{n}" for n in range(600)]
        STATE.relations[("mod", mod_id, "tags")] = list(names)

        before = len(STATE.requests)

        self.run_cli("tags", "rm", "mod", str(mod_id), *names)

        sent = [r for r in STATE.requests[before:] if r[0] == "DELETE"]

        self.assertEqual(len(sent), 2)
        self.assertEqual(STATE.relations[("mod", mod_id, "tags")], [])


class TestAnonymous(CliTestCase):
    """The unauthenticated read surface.

    Its whole contract rests on one thing — that no `Authorization` header is
    sent — so the first test asserts that directly rather than inferring it from
    a successful response.
    """

    def test_sends_no_authorization_header(self) -> None:
        """Not an empty header — none at all.

        `IsAnonRequest` keys on the header being absent. An empty or malformed
        one is the KEYED surface being told the key is rubbish, which answers
        401 and would 404 items the caller can plainly see in a browser.
        """

        self.seed_mod(name="Public Mod", url="public-mod")

        self.run_anon("mod", "list", "-o", "json")

        self.assertEqual(STATE.auth_headers, [None])

    def test_get_returns_a_summary_not_the_record(self) -> None:
        mod_id = self.seed_mod(
            name="Public Mod", url="public-mod", content="the whole body"
        )

        out, _err = self.run_anon("mod", "get", str(mod_id), "-o", "json")
        row = json.loads(out)

        self.assertEqual(row["type"], "mod")
        self.assertEqual(row["name"], "Public Mod")
        self.assertEqual(row["slug"], "public-mod")
        self.assertIn("stats", row)

        # The summary is not the row. `content` is the expensive half and the
        # surface never selects it; `hidden` cannot be answered at all.
        self.assertNotIn("content", row)
        self.assertNotIn("hidden", row)

    def test_hidden_item_is_404_not_403(self) -> None:
        mod_id = self.seed_mod(name="Draft", hidden=True)

        err = self.run_anon("mod", "get", str(mod_id), expect=4)[1]

        self.assertIn("not_found", err)

    def test_api_disabled_says_so(self) -> None:
        """`apiPublic: false` is a 403 that names itself, not a 404."""

        mod_id = self.seed_mod(name="Opted Out", apiPublic=False)

        err = self.run_anon("mod", "get", str(mod_id), expect=3)[1]

        self.assertIn("api_disabled", err)
        # The hint has to point at the fix, which is a key rather than a retry.
        self.assertIn("--anon", err)

    def test_relation_types_need_a_key(self) -> None:
        err = self.run_anon("content", "comment", "list", expect=2)[1]

        self.assertIn("cannot be read without a key", err)

    def test_writes_are_refused_before_the_request(self) -> None:
        before = len(STATE.requests)

        # An asset needs only `name`, so this clears the local required-field
        # check and reaches the transport — which is what is under test.
        err = self.run_anon("asset", "create", "--set", "name=Nope", expect=2)[1]

        self.assertIn("read-only", err)
        # Refused locally: sending it would be a 401 that blames the key.
        self.assertEqual(len(STATE.requests), before)

    def test_relations_are_refused(self) -> None:
        mod_id = self.seed_mod()

        err = self.run_anon("tags", "list", "mod", str(mod_id), expect=2)[1]

        self.assertIn("anonymous surface", err)

    def test_files_are_refused(self) -> None:
        err = self.run_anon("file", "get", "some-id", expect=2)[1]

        self.assertIn("always need a key", err)

    def test_list_drops_the_filters_it_cannot_serve(self) -> None:
        self.seed_mod(name="Alpha", appId=1)
        self.seed_mod(name="Beta", appId=2)

        out, err = self.run_anon(
            "mod", "list", "--search", "Alpha", "--mine", "--app", "1", "-o", "json"
        )

        # Said out loud: an ignored filter looks exactly like one that matched
        # everything, which is the failure mode worth catching.
        self.assertIn("anonymous listings take no", err)
        self.assertIn("search", err)
        self.assertIn("mine", err)

        rows = json.loads(out)

        # `appId` is the one filter it DOES serve, so it still applied.
        self.assertEqual([r["name"] for r in rows], ["Alpha"])

    def test_official_filter_narrows_the_listing(self) -> None:
        """The second anonymous filter. For articles this one IS the blog."""

        self.seed_mod(name="Site Post", isOfficial=True)
        self.seed_mod(name="Member Post", isOfficial=False)

        out, _err = self.run_anon("mod", "list", "--official", "-o", "json")
        self.assertEqual([r["name"] for r in json.loads(out)], ["Site Post"])

        # `--no-official` is the other half: everything a member published.
        out, _err = self.run_anon("mod", "list", "--no-official", "-o", "json")
        self.assertEqual([r["name"] for r in json.loads(out)], ["Member Post"])

    def test_official_is_ignored_on_the_keyed_list(self) -> None:
        """It is an anonymous-only filter, and the keyed list drops it loudly.

        Silence would be the same trap as a dropped `--search`: the keyed
        handler ignores an unknown query param, so an unfiltered page would
        read as a filter that matched everything.
        """

        self.seed_mod(name="Site Post", isOfficial=True)
        self.seed_mod(name="Member Post", isOfficial=False)

        err = self.run_cli_err("mod", "list", "--official", "-o", "json", expect=0)

        self.assertIn("no official filter", err)

        # Dropped locally rather than sent and ignored, so the query the server
        # saw carries no trace of it.
        sent = [path for method, path in STATE.requests if method == "GET"]
        self.assertTrue(sent)
        self.assertNotIn("official", " ".join(sent))

    def test_list_reports_the_servers_note(self) -> None:
        self.seed_mod(name="Alpha")

        _out, err = self.run_anon("mod", "list", "-o", "json")

        self.assertIn("Anonymous listings are summaries", err)

    def test_a_429_is_retried(self) -> None:
        self.seed_mod(name="Alpha")
        STATE.anon_rate_limit_for = 1

        out, _err = self.run_anon("mod", "list", "-o", "json", "--retries", "2")

        self.assertEqual(len(json.loads(out)), 1)

    def test_retry_after_header_beats_the_sentence(self) -> None:
        """A header is a better answer than a regex over English.

        Both are present on an anonymous 429 and they can disagree — the
        sentence is rendered from a rounded reset, the header from the real one.
        """

        from tmc_cli.errors import ApiError
        from tmc_cli.http import Transport

        transport = Transport(self.server.base_url, _NoCredential(), retries=1)

        err = ApiError(429, "Rate limit exceeded. Try again in 900s.", retry_after=3)

        self.assertEqual(transport._retry_delay(err, "GET", 1), 3)

        # With no header, the sentence is still the fallback.
        bare = ApiError(429, "Rate limit exceeded. Try again in 7s.")

        self.assertEqual(transport._retry_delay(bare, "GET", 1), 7)

    def test_site_switch_off_is_auth_required(self) -> None:
        STATE.anon_enabled = False
        self.seed_mod()

        err = self.run_anon("mod", "list", expect=3)[1]

        self.assertIn("auth_required", err)

    def test_a_key_still_reads_the_whole_record(self) -> None:
        """The keyed surface is untouched by any of the above."""

        mod_id = self.seed_mod(name="Public Mod", content="the whole body")

        row = json.loads(self.run_cli("mod", "get", str(mod_id), "-o", "json"))

        self.assertEqual(row["content"], "the whole body")


class TestTransport(CliTestCase):
    def test_retries_after_a_rate_limit(self) -> None:
        self.seed_mod(name="Eventually")
        STATE.rate_limit_for = 2

        output = self.run_cli("mod", "list", "-o", "ids")

        self.assertTrue(output.strip())
        self.assertEqual(STATE.rate_limit_for, 0)

    def test_gives_up_when_the_wait_exceeds_the_cap(self) -> None:
        STATE.rate_limit_for = 5

        stderr = self.run_cli_err(
            "--retry-wait-max", "0.5", "mod", "list", expect=6
        )

        self.assertIn("Rate limit exceeded", stderr)
        self.assertIn("--retry-wait-max", stderr)

    def test_404_maps_to_its_own_exit_code(self) -> None:
        self.run_cli_err("mod", "get", "424242", expect=4)

    def test_unknown_type_is_a_usage_error(self) -> None:
        stderr = self.run_cli_err("content", "nope", "list", expect=2)

        self.assertIn("invalid choice", stderr.lower())

    def test_raw_escape_hatch(self) -> None:
        mod_id = self.seed_mod(name="Raw")

        rows = json.loads(self.run_cli("raw", "GET", f"/api/content/mod/{mod_id}", "-o", "json"))

        self.assertEqual(rows["name"], "Raw")


class _NoCredential:
    """Enough of a Credential for a transport that never sends a request."""

    kind = "anonymous"

    def authorization(self) -> None:
        return None


def _pkcs8_pem(seed: bytes) -> str:
    """Wrap a raw seed as a PKCS#8 PEM, so the loader is exercised end to end."""

    import base64

    der = bytes([0x30, 0x2E, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2B, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20]) + seed
    body = base64.b64encode(der).decode("ascii")

    return f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n"


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SecretFileModeTests(unittest.TestCase):
    """Files holding a credential must be 0600 from the moment they exist.

    Asserting the mode of the *finished* file is not enough: the bug these cover
    was a file created at the umask default and chmodded afterwards, which is
    correct at rest and world-readable while it is being written.
    """

    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self._old = os.environ.get("TMC_CONFIG_DIR")
        os.environ["TMC_CONFIG_DIR"] = self._dir
        # A permissive umask, so a mode that came from the umask is visibly wrong.
        self._old_umask = os.umask(0o022)

    def tearDown(self) -> None:
        os.umask(self._old_umask)

        if self._old is None:
            os.environ.pop("TMC_CONFIG_DIR", None)
        else:
            os.environ["TMC_CONFIG_DIR"] = self._old

        shutil.rmtree(self._dir, ignore_errors=True)

    def test_open_private_creates_at_0600(self) -> None:
        from tmc_cli.config import open_private

        path = os.path.join(self._dir, "secret")

        with open_private(path) as handle:
            # Checked while the handle is still open — the point is that the
            # window between create and close is never readable by others.
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            handle.write("tmc_deadbeef")

        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_open_private_narrows_a_pre_existing_file(self) -> None:
        """A stale temp file from an interrupted save keeps its old mode."""

        from tmc_cli.config import open_private

        path = os.path.join(self._dir, "stale")
        open(path, "w").close()
        os.chmod(path, 0o644)

        with open_private(path) as handle:
            handle.write("tmc_deadbeef")

        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_config_save_never_exposes_the_temp_file(self) -> None:
        from tmc_cli.config import Config, Profile, config_path

        config = Config()
        config.profiles["default"] = Profile(name="default", token="tmc_secret")

        tmp_seen: list[int] = []
        real_replace = os.replace

        def spy(src, dst):
            # The temp file is still the only copy of the token at this point.
            tmp_seen.append(stat.S_IMODE(os.stat(src).st_mode))
            return real_replace(src, dst)

        with mock.patch("tmc_cli.config.os.replace", spy):
            path = config.save()

        self.assertEqual(tmp_seen, [0o600])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(path, config_path())

    def test_config_save_leaves_no_temp_file_behind_on_failure(self) -> None:
        from tmc_cli.config import Config, Profile, config_path

        config = Config()
        config.profiles["default"] = Profile(name="default", token="tmc_secret")

        with mock.patch("tmc_cli.config.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                config.save()

        self.assertFalse(os.path.exists(f"{config_path()}.tmp"))

    def test_copy_key_writes_the_pem_at_0600(self) -> None:
        import base64
        import textwrap

        from tmc_cli.config import config_path
        from tmc_cli.ed25519 import generate_seed

        # PKCS#8 Ed25519: a fixed 16-byte header, then the 32-byte seed.
        der = bytes.fromhex("302e020100300506032b657004220420") + generate_seed()
        body = "\n".join(textwrap.wrap(base64.b64encode(der).decode(), 64))
        source = os.path.join(self._dir, "source.pem")

        with open(source, "w", encoding="utf-8") as handle:
            handle.write(
                f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n"
            )

        rc = cli.main(
            [
                "auth",
                "login",
                "--profile",
                "copied",
                "--jwt",
                "--key-id",
                "tmcak_test",
                "--private-key",
                source,
                "--copy-key",
                "--no-verify",
            ]
        )

        self.assertEqual(rc, 0)

        destination = os.path.join(os.path.dirname(config_path()), "copied.pem")
        self.assertTrue(os.path.exists(destination))
        self.assertEqual(stat.S_IMODE(os.stat(destination).st_mode), 0o600)


class RedactAuthorizationTests(unittest.TestCase):
    """`--debug` goes to stderr, and stderr is what CI archives."""

    def test_no_bearer_secret_survives_redaction(self) -> None:
        from tmc_cli.http import _redact_authorization

        token = "tmc_" + "a1b2c3d4" * 4
        line = _redact_authorization(f"Bearer {token}")

        self.assertIn("Bearer", line)
        self.assertIn("tmc_", line)
        # The old `[:24]` slice printed thirteen characters of the hex.
        self.assertNotIn(token[4:], line)
        self.assertNotIn(token[4:8], line)
        self.assertIn(str(len(token)), line)

    def test_a_jwt_assertion_is_redacted_too(self) -> None:
        from tmc_cli.http import _redact_authorization

        line = _redact_authorization("Bearer eyJhbGciOiJFZERTQSJ9.body.signature")

        self.assertTrue(line.startswith("Bearer eyJh"))
        self.assertNotIn("signature", line)

    def test_a_header_with_no_scheme_is_redacted_whole(self) -> None:
        from tmc_cli.http import _redact_authorization

        line = _redact_authorization("tmc_barenakedsecret")

        self.assertNotIn("tmc_", line)
        self.assertNotIn("barenakedsecret", line)
