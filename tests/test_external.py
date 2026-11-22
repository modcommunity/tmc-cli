"""`tmc mods`: link parsing, provider mapping, verified install, the store. No network.

The network is `Fetch`; here it is a fake that serves canned JSON and bytes, so
nothing opens a socket. Keyed providers go through `AppSession`, which is patched.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tmc_cli import cli  # noqa: E402
from tmc_cli.commands import mods_cmd  # noqa: E402
from tmc_cli.errors import ApiError  # noqa: E402
from tmc_cli.external import install as store  # noqa: E402
from tmc_cli.external import links  # noqa: E402
from tmc_cli.external.parse import ParseError, parse_input  # noqa: E402
from tmc_cli.external.resolve import (  # noqa: E402
    ProviderError,
    RemoteFile,
    Resolved,
    map_github,
    map_modrinth,
    map_site,
    map_thunderstore,
    resolve_direct,
)


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)

    return buf.getvalue()


class FakeFetch:
    def __init__(self, json_by_url=None, blobs=None) -> None:
        self.json_by_url = json_by_url or {}
        self.blobs = blobs or {}
        self.requested: list[str] = []

    def get_json(self, url):
        self.requested.append(url)

        if url not in self.json_by_url:
            raise ProviderError(f"unexpected {url}")

        return self.json_by_url[url]

    def download(self, url, dest, *, max_bytes, progress):
        self.requested.append(url)
        data = self.blobs[url]

        if max_bytes and len(data) > max_bytes:
            raise ProviderError("over the limit (--max-mb; 0 means no limit)")

        with open(dest, "wb") as f:
            f.write(data)

        return len(data)


class ParseCase(unittest.TestCase):
    def test_modrinth(self) -> None:
        r = parse_input("https://modrinth.com/mod/sodium/version/abc")
        self.assertEqual((r.provider, r.project, r.version), ("modrinth", "sodium", "abc"))

    def test_http_refused(self) -> None:
        with self.assertRaises(ParseError):
            parse_input("http://modrinth.com/mod/sodium")

    def test_github_release_asset(self) -> None:
        r = parse_input("https://github.com/o/r/releases/download/v1/a.zip")
        self.assertEqual((r.provider, r.project, r.version), ("github", "o/r", "v1"))

    def test_direct_with_hash_fragment(self) -> None:
        r = parse_input("https://example.com/m.zip#sha256=" + "a" * 64)
        self.assertEqual(r.provider, "direct")

    def test_schemes(self) -> None:
        self.assertEqual(parse_input("curseforge://install?addonId=1&fileId=2").provider, "curseforge")
        self.assertEqual(parse_input("steam://url/CommunityFilePage/123").provider, "steam")

    def test_nxm_grant_is_not_in_the_stored_url(self) -> None:
        raw = "nxm://skyrim/mods/1/files/2?key=SECRET&expires=9&user_id=7"
        r = parse_input(raw)
        self.assertEqual(r.provider, "nexus")
        self.assertNotIn("SECRET", store.stored_url(raw))
        self.assertNotIn("expires", store.stored_url(raw))

    def test_garbage(self) -> None:
        with self.assertRaises(ParseError):
            parse_input("not a link")

    def test_pasted_line_forms(self) -> None:
        r = parse_input("# a comment\n[InternetShortcut]\nURL=https://modrinth.com/mod/sodium\n")
        self.assertEqual(r.project, "sodium")


class MapCase(unittest.TestCase):
    def test_modrinth_hashes(self) -> None:
        res = map_modrinth(
            {"id": "P", "slug": "sodium", "title": "Sodium", "project_type": "mod"},
            {"id": "V", "version_number": "1.0", "files": [
                {"url": "https://cdn.modrinth.com/a.jar", "filename": "a.jar", "size": 3, "primary": True, "hashes": {"sha512": "ab"}}]},
        )
        self.assertEqual(res.files[0].hashes["sha512"], "ab")
        self.assertEqual(res.project_id, "P")

    def test_modrinth_no_files(self) -> None:
        with self.assertRaises(ProviderError):
            map_modrinth({"id": "P"}, {"id": "V", "files": []})

    def test_site_rejects_other_provider_and_http(self) -> None:
        with self.assertRaises(ProviderError):
            map_site("curseforge", {"provider": "modio", "files": []})

        with self.assertRaises(ProviderError):
            map_site("curseforge", {"provider": "curseforge", "files": [{"url": "http://x/a.zip", "name": "a.zip"}]})

    def test_site_manual_only(self) -> None:
        r = map_site("curseforge", {"provider": "curseforge", "projectId": "1", "versionId": "2", "title": "T", "manualOnly": "https://cf/p"})
        self.assertEqual(r.manual_only, "https://cf/p")

    def test_direct_fragment(self) -> None:
        r = resolve_direct(parse_input("https://e.com/m.zip#sha256=" + "b" * 64))
        self.assertEqual(r.files[0].hashes["sha256"], "b" * 64)
        self.assertNotIn("#", r.files[0].url)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "store")

    def resolved(self, name: str, data: bytes, **hashes) -> tuple[Resolved, RemoteFile, FakeFetch]:
        f = RemoteFile("https://x/" + name, name, len(data), hashes, True)
        return Resolved("direct", "p1", "v1", "Pack", "1.0", files=[f]), f, FakeFetch(blobs={f.url: data})

    def test_zip_install_verifies_and_records(self) -> None:
        data = make_zip({"a/b.txt": b"hi"})
        res, f, fetch = self.resolved("m.zip", data, sha256=hashlib.sha256(data).hexdigest())
        got = store.install(res, f, "https://x/m.zip?key=SECRET", fetch, self.root, max_bytes=0)
        self.assertEqual(got.verified, "sha256")
        self.assertTrue(os.path.isfile(os.path.join(got.path, "a", "b.txt")))
        entry = store.load_manifest(self.root)["mods"][0]
        self.assertNotIn("SECRET", json.dumps(entry))

    def test_hash_mismatch_installs_nothing(self) -> None:
        data = b"payload"
        res, f, fetch = self.resolved("m.jar", data, sha512="00")
        with self.assertRaises(ProviderError):
            store.install(res, f, "https://x/m.jar", fetch, self.root, max_bytes=0)
        self.assertEqual(store.load_manifest(self.root)["mods"], [])
        self.assertEqual([d for d in os.listdir(self.root) if not d.startswith(".")], [])

    def test_size_mismatch(self) -> None:
        res, f, fetch = self.resolved("m.jar", b"abc")
        f.size = 99
        with self.assertRaises(ProviderError):
            store.install(res, f, "https://x/m.jar", fetch, self.root, max_bytes=0)

    def test_zip_slip_refused(self) -> None:
        data = make_zip({"../evil.txt": b"x"})
        res, f, fetch = self.resolved("m.zip", data)
        with self.assertRaises(ProviderError):
            store.install(res, f, "https://x/m.zip", fetch, self.root, max_bytes=0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil.txt")))

    def test_cap_and_zero_means_unlimited(self) -> None:
        data = b"z" * (2 * 1024 * 1024)
        res, f, fetch = self.resolved("big.jar", data)
        with self.assertRaises(ProviderError) as cm:
            store.install(res, f, "https://x/big.jar", fetch, self.root, max_bytes=1024 * 1024)
        self.assertIn("--max-mb", str(cm.exception))
        self.assertEqual(store.install(res, f, "https://x/big.jar", fetch, self.root, max_bytes=0).files, ["big.jar"])

    def test_reinstall_replaces_entry_and_remove(self) -> None:
        res, f, fetch = self.resolved("m.jar", b"one")
        store.install(res, f, "u", fetch, self.root, max_bytes=0)
        res.version_id = "v2"
        again = store.install(res, f, "u", fetch, self.root, max_bytes=0)
        self.assertTrue(again.replaced)
        data = store.load_manifest(self.root)
        self.assertEqual(len(data["mods"]), 1)
        store.remove(self.root, data["mods"][0])
        self.assertEqual(store.load_manifest(self.root)["mods"], [])

    def test_remove_never_deletes_outside_store(self) -> None:
        outside = os.path.join(self.tmp.name, "keep")
        os.makedirs(outside)
        store._remove_dir(self.root, outside)
        self.assertTrue(os.path.isdir(outside))

    def test_unsafe_file_name(self) -> None:
        with self.assertRaises(ProviderError):
            store.safe_name("..")
        self.assertEqual(store.safe_name("../../x.zip"), "x.zip")


class CommandCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "store")

    def run_cli(self, *argv: str, fetch: FakeFetch | None = None, keyed: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()

        with mock.patch.object(mods_cmd, "_fetcher", return_value=fetch or FakeFetch()), \
                mock.patch.object(mods_cmd, "keyed_enabled", return_value=keyed), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["mods", *argv])

        return code, out.getvalue(), err.getvalue()

    def modrinth_fetch(self, blob: bytes) -> FakeFetch:
        digest = hashlib.sha512(blob).hexdigest()
        return FakeFetch(
            {
                "https://api.modrinth.com/v2/project/sodium": {"id": "P", "slug": "sodium", "title": "Sodium"},
                "https://api.modrinth.com/v2/project/sodium/version": [
                    {"id": "V1", "version_number": "1.0", "files": [
                        {"url": "https://cdn.modrinth.com/s.jar", "filename": "s.jar", "size": len(blob), "primary": True, "hashes": {"sha512": digest}}]}],
            },
            {"https://cdn.modrinth.com/s.jar": blob},
        )

    def test_add_list_remove(self) -> None:
        fetch = self.modrinth_fetch(b"jar")
        code, out, _ = self.run_cli("add", "https://modrinth.com/mod/sodium", "--dest", self.root, "-o", "json", fetch=fetch)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["verified"], "sha512")

        code, out, _ = self.run_cli("list", "--dest", self.root, "-o", "json")
        self.assertEqual(json.loads(out)[0]["id"], "modrinth:P")

        code, _, _ = self.run_cli("remove", "sodium", "--dest", self.root, "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(store.load_manifest(self.root)["mods"], [])

    def test_dry_run_downloads_nothing(self) -> None:
        fetch = self.modrinth_fetch(b"jar")
        code, out, _ = self.run_cli("add", "https://modrinth.com/mod/sodium", "--dest", self.root, "--dry-run", fetch=fetch)
        self.assertEqual(code, 0)
        self.assertNotIn("https://cdn.modrinth.com/s.jar", fetch.requested)
        self.assertFalse(os.path.exists(self.root))

    def test_curseforge_off_is_exit_9_without_secrets(self) -> None:
        code, _, err = self.run_cli("add", "curseforge://install?addonId=1&fileId=2")
        self.assertEqual(code, 9)
        self.assertIn("not enabled", err)

    def test_nexus_is_exit_9_and_never_echoes_the_grant(self) -> None:
        code, out, err = self.run_cli("add", "nxm://skyrim/mods/1/files/2?key=SECRET&expires=9&user_id=7")
        self.assertEqual(code, 9)
        self.assertNotIn("SECRET", out + err)

    def test_curseforge_on_resolves_through_the_site(self) -> None:
        blob = b"cf"
        body = {"provider": "curseforge", "projectId": "1", "versionId": "2", "title": "CF Mod",
                "files": [{"url": "https://edge.forgecdn.net/a.jar", "name": "a.jar", "size": 2, "primary": True}]}
        fetch = FakeFetch(blobs={"https://edge.forgecdn.net/a.jar": blob})
        session = mock.Mock()
        session.request.return_value = body

        with mock.patch.object(mods_cmd, "AppSession", return_value=session):
            code, _, err = self.run_cli("add", "curseforge://install?addonId=1&fileId=2", "--dest", self.root, fetch=fetch, keyed=True)

        self.assertEqual(code, 0, err)

    def test_keyed_check_treats_api_error_as_off(self) -> None:
        session = mock.Mock()
        session.request.side_effect = ApiError(401, "no")

        with mock.patch.object(mods_cmd, "AppSession", return_value=session):
            self.assertFalse(mods_cmd.keyed_enabled(mock.Mock(), "curseforge"))

    def test_steam_is_a_hand_off_exit_10(self) -> None:
        code, _, err = self.run_cli("add", "steam://url/CommunityFilePage/123")
        self.assertEqual(code, 10)
        self.assertIn("steamcommunity.com", err)

    def test_local_file(self) -> None:
        src = os.path.join(self.tmp.name, "m.zip")

        with open(src, "wb") as f:
            f.write(make_zip({"x.txt": b"1"}))

        code, _, err = self.run_cli("add", src, "--dest", self.root)
        self.assertEqual(code, 0, err)
        self.assertEqual(store.load_manifest(self.root)["mods"][0]["provider"], "local")

    def test_max_mb_negative_is_usage_error(self) -> None:
        code, _, _ = self.run_cli("add", "https://e.com/m.zip", "--max-mb", "-1", "--dest", self.root)
        self.assertEqual(code, 2)

    def test_remove_unknown(self) -> None:
        code, _, err = self.run_cli("remove", "nope", "--dest", self.root, "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("no installed mod", err)


class LinksCase(unittest.TestCase):
    def test_linux_states(self) -> None:
        def run(argv):
            scheme = argv[-1]
            return {"x-scheme-handler/nxm": "vortex.desktop\n", "x-scheme-handler/curseforge": "gamespice-app.desktop\n"}.get(scheme, "\n")

        rows = {r["scheme"]: r for r in links.all_status(run, "linux")}
        self.assertEqual(rows["nxm"]["state"], "registered")
        self.assertEqual(rows["nxm"]["ours"], "no")
        self.assertEqual(rows["curseforge"]["ours"], "yes")
        self.assertEqual(rows["modrinth"]["state"], "free")

    def test_tool_missing_is_unknown(self) -> None:
        self.assertEqual(links.status("nxm", lambda argv: None, "linux")["state"], "unknown")

    def test_macos_unknown(self) -> None:
        self.assertEqual(links.status("nxm", lambda argv: "", "darwin")["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
