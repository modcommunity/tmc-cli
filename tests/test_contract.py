"""The contract: caching it, overlaying it, and diffing against it.

No network here. `fetch` is the one thing that talks to a site, and it is thin
enough to be worth less than the test would cost; everything that decides
BEHAVIOUR — which cached document may be trusted, what the overlay does to
`TYPES`, and what counts as drift — is pure and is what these cover.

The diff tests matter most. Every one of them is a mistake the first version of
this feature actually made: reporting `mod edit` as missing when the CLI has
`mod update`, reporting `help` as missing when a shell has one, and reporting the
CLI's stricter integer fields as drift forever, which would have made the check
something everybody learned to ignore.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from tmc_cli import contract, schema  # noqa: E402
from tmc_cli.schema import BOOL, INT, STR, TYPES  # noqa: E402


def doc(**over):
    base = {
        "contract": 1,
        "console": "1.0.0",
        "generatedAt": "2026-09-21T00:00:00.000Z",
        "types": [],
        "commands": [],
        "_baseUrl": "https://example.test",
        "_fetchedAt": int(time.time()),
    }
    base.update(over)

    return base


class UsableFor(unittest.TestCase):
    def test_none_is_not_usable(self):
        self.assertFalse(contract.usable_for(None, "https://example.test"))

    def test_same_site_is_usable(self):
        self.assertTrue(contract.usable_for(doc(), "https://example.test"))

    def test_trailing_slash_does_not_matter(self):
        self.assertTrue(contract.usable_for(doc(), "https://example.test/"))

    def test_another_site_is_not_usable(self):
        # The whole point: a dev checkout and production have different
        # registries, and judging one by the other is worse than no contract.
        self.assertFalse(contract.usable_for(doc(), "https://other.test"))

    def test_stale_is_not_usable(self):
        old = doc(_fetchedAt=int(time.time()) - (contract.MAX_AGE_DAYS + 1) * 86400)

        self.assertFalse(contract.usable_for(old, "https://example.test"))


class Cache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.prev = os.environ.get("TMC_CONFIG_DIR")
        os.environ["TMC_CONFIG_DIR"] = self.dir

    def tearDown(self):
        if self.prev is None:
            os.environ.pop("TMC_CONFIG_DIR", None)
        else:
            os.environ["TMC_CONFIG_DIR"] = self.prev

    def test_round_trip(self):
        contract.save(doc(console="9.9.9"))

        loaded = contract.load()

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["console"], "9.9.9")

    def test_missing_cache_is_none(self):
        self.assertIsNone(contract.load())

    def test_corrupt_cache_is_none_not_an_error(self):
        # A half-written file must not stop the CLI from running at all.
        with open(contract.contract_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")

        self.assertIsNone(contract.load())

    def test_cache_is_private(self):
        contract.save(doc())

        mode = os.stat(contract.contract_path()).st_mode & 0o777

        self.assertEqual(mode, 0o600)

    def test_clear(self):
        contract.save(doc())

        self.assertTrue(contract.clear())
        self.assertIsNone(contract.load())


class SpecUrl(unittest.TestCase):
    def test_bare_origin(self):
        self.assertEqual(
            contract.spec_url("https://moddingcommunity.com"),
            "https://moddingcommunity.com/api/content/spec",
        )

    def test_api_origin(self):
        # `api.moddingcommunity.com` serves the content API under /content; a
        # base url that already ends in /api must not gain a second one.
        self.assertEqual(
            contract.spec_url("https://api.moddingcommunity.com/api"),
            "https://api.moddingcommunity.com/api/content/spec",
        )

    def test_trailing_slash(self):
        self.assertEqual(
            contract.spec_url("https://x.test/"),
            "https://x.test/api/content/spec",
        )


class Overlay(unittest.TestCase):
    def tearDown(self):
        schema.clear_override()

    def test_adds_a_field_the_mirror_lacks(self):
        merged = contract.merged_types(
            doc(
                types=[
                    {
                        "name": "mod",
                        "create": [
                            {"name": "name", "type": "string", "required": True},
                            {"name": "brandNew", "type": "bool", "required": False},
                        ],
                        "relations": ["tags"],
                        "canonical": True,
                        "staffOnlyWrite": False,
                    }
                ]
            )
        )

        names = merged["mod"].field_names()

        self.assertIn("brandNew", names)
        self.assertEqual(merged["mod"].get("brandNew").type, BOOL)

    def test_keeps_the_mirror_s_note_and_enum(self):
        # The contract carries neither, and losing them would make
        # `tmc schema` worse in exchange for nothing.
        before = TYPES["asset"].get("license")

        merged = contract.merged_types(
            doc(
                types=[
                    {
                        "name": "asset",
                        "create": [{"name": "license", "type": "string"}],
                        "relations": [],
                        "canonical": True,
                        "staffOnlyWrite": False,
                    }
                ]
            )
        )

        after = merged["asset"].get("license")

        self.assertEqual(after.enum, before.enum)
        self.assertEqual(after.note, before.note)

    def test_a_type_the_mirror_has_and_the_site_does_not_survives(self):
        merged = contract.merged_types(doc(types=[]))

        # Kept so `tmc schema` can still describe it; the DRIFT report is where
        # "the site dropped this" gets said.
        self.assertIn("mod", merged)

    def test_install_changes_what_spec_for_answers(self):
        contract.install(
            doc(
                types=[
                    {
                        "name": "mod",
                        "create": [{"name": "onlyField", "type": "string"}],
                        "relations": [],
                        "canonical": True,
                        "staffOnlyWrite": False,
                    }
                ]
            )
        )

        self.assertEqual(schema.spec_for("mod").field_names(), ["onlyField"])

        schema.clear_override()

        self.assertIn("name", schema.spec_for("mod").field_names())


def cmd(path, **over):
    entry = {"path": path, "help": ""}
    entry.update(over)

    return entry


class Diff(unittest.TestCase):
    def test_clean_when_they_agree(self):
        report = contract.diff(
            doc(commands=[cmd(["mod", "update"])]),
            {("mod", "update")},
        )

        self.assertEqual(report["commands"]["added"], [])
        self.assertEqual(report["commands"]["removed"], [])

    def test_an_alias_counts_as_having_the_command(self):
        # The CLI says `update`; the console also answers to `edit`. One command.
        report = contract.diff(
            doc(commands=[cmd(["mod", "update"], aliases=["edit"])]),
            {("mod", "edit")},
        )

        self.assertEqual(report["commands"]["added"], [])
        self.assertEqual(report["commands"]["removed"], [])

    def test_a_local_command_is_not_a_gap(self):
        # A shell already has `help`; reporting it forever would train people to
        # ignore the report.
        report = contract.diff(doc(commands=[cmd(["help"], local=True)]), set())

        self.assertEqual(report["commands"]["added"], [])

    def test_a_real_missing_command_is_reported(self):
        report = contract.diff(doc(commands=[cmd(["open"])]), set())

        self.assertEqual(report["commands"]["added"], ["open"])

    def test_a_cli_only_command_is_reported(self):
        report = contract.diff(doc(commands=[]), {("mod", "invented")})

        self.assertEqual(report["commands"]["removed"], ["mod invented"])

    def test_the_cli_only_allowlist_is_silent(self):
        report = contract.diff(doc(commands=[]), {("completion",)})

        self.assertEqual(report["commands"]["removed"], [])

    def test_being_stricter_is_not_drift(self):
        # The server declares most id columns `z.number()` with no `.int()`. The
        # mirror says int. That is a choice, and it must not fail the check.
        report = contract.diff(
            doc(
                types=[
                    {
                        "name": "mod",
                        "create": [{"name": "appId", "type": "float"}],
                        "relations": list(TYPES["mod"].relations),
                        "canonical": True,
                        "staffOnlyWrite": False,
                    }
                ]
            ),
            set(),
        )

        self.assertEqual(report["fields"]["retyped"], {})
        self.assertIn("mod", report["fields"]["narrowed"])

    def test_a_real_retype_is_drift(self):
        report = contract.diff(
            doc(
                types=[
                    {
                        "name": "mod",
                        "create": [{"name": "name", "type": "bool"}],
                        "relations": list(TYPES["mod"].relations),
                        "canonical": True,
                        "staffOnlyWrite": False,
                    }
                ]
            ),
            set(),
        )

        self.assertIn("mod", report["fields"]["retyped"])

    def test_is_clean_ignores_narrowing_only(self):
        narrowed = {
            "types": {"added": [], "removed": []},
            "fields": {
                "added": {},
                "removed": {},
                "retyped": {},
                "narrowed": {"mod": ["appId (int → float)"]},
            },
            "relations": {},
            "commands": {"added": [], "removed": []},
        }

        self.assertTrue(contract.is_clean(narrowed))

        narrowed["fields"]["retyped"] = {"mod": ["name (str → bool)"]}

        self.assertFalse(contract.is_clean(narrowed))


class TypeMapping(unittest.TestCase):
    def test_every_site_type_name_maps(self):
        # The two vocabularies were written independently. If the site grows a
        # name this does not know, the field silently becomes a string.
        for name in (
            "string",
            "int",
            "float",
            "bool",
            "date",
            "json",
            "strList",
            "intList",
            "unknown",
        ):
            self.assertIn(name, contract.TYPE_MAP)

    def test_unknown_falls_back_to_string(self):
        self.assertEqual(contract.TYPE_MAP["unknown"], STR)

    def test_int_stays_int(self):
        self.assertEqual(contract.TYPE_MAP["int"], INT)


if __name__ == "__main__":
    unittest.main()
