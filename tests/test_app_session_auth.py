"""The app sign-in as the fallback credential for mod/creator commands."""

import argparse
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tmc_cli import cli  # noqa: E402
from tmc_cli.auth import AnonymousCredential, AppSessionCredential, BearerCredential  # noqa: E402
from tmc_cli.config import Config, Profile, resolve  # noqa: E402
from tmc_cli.errors import ConfigError  # noqa: E402


def _args(**kw):
    ns = argparse.Namespace(profile=None, base_url=None, token=None, anon=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class AppSessionCredentialTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self._env = mock.patch.dict(
            os.environ,
            {k: v for k, v in os.environ.items() if not k.startswith("TMC_")},
            clear=True,
        )
        self._env.start()
        os.environ["TMC_CONFIG_DIR"] = self._dir

    def tearDown(self) -> None:
        self._env.stop()

    def test_env_app_token_used_when_nothing_else(self) -> None:
        os.environ["TMC_APP_TOKEN"] = "tmc_at_abc"
        cred = resolve(_args()).credential
        self.assertIsInstance(cred, AppSessionCredential)
        self.assertEqual(cred.authorization(), "Bearer tmc_at_abc")

    def test_explicit_token_wins(self) -> None:
        os.environ["TMC_APP_TOKEN"] = "tmc_at_abc"
        cred = resolve(_args(token="tmc_key")).credential
        self.assertIsInstance(cred, BearerCredential)

    def test_anon_wins(self) -> None:
        os.environ["TMC_APP_TOKEN"] = "tmc_at_abc"
        self.assertIsInstance(resolve(_args(anon=True)).credential, AnonymousCredential)

    def test_profile_key_wins_over_app_token(self) -> None:
        os.environ["TMC_APP_TOKEN"] = "tmc_at_abc"
        config = Config.load()
        config.profiles["default"] = Profile(name="default", token="tmc_stored")
        config.save()
        self.assertIsInstance(resolve(_args()).credential, BearerCredential)

    def test_stored_app_session_used_for_same_base_url(self) -> None:
        config = Config.load()
        config.profiles["default"] = Profile(
            name="default",
            app={"base_url": "https://x.test", "access_token": "tmc_at_stored"},
        )
        config.save()
        cred = resolve(_args(base_url="https://x.test/")).credential
        self.assertIsInstance(cred, AppSessionCredential)
        self.assertEqual(cred.authorization(), "Bearer tmc_at_stored")

    def test_stored_app_session_not_sent_to_other_host(self) -> None:
        config = Config.load()
        config.profiles["default"] = Profile(
            name="default",
            app={"base_url": "https://x.test", "access_token": "tmc_at_stored"},
        )
        config.save()
        with self.assertRaises(ConfigError):
            resolve(_args(base_url="https://other.test"))

    def test_hint_mentions_app_sign_in(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            resolve(_args())
        self.assertIn("GameSpice app", ctx.exception.hint)
        self.assertIn("TMC_APP_TOKEN", ctx.exception.hint)
        self.assertNotIn("\\u", ctx.exception.hint)
        self.assertNotIn("\\x", ctx.exception.hint)


class SafeStreamsTest(unittest.TestCase):
    def test_non_utf8_pipe_does_not_raise(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with mock.patch.object(sys, "stdout", stream), mock.patch.object(sys, "stderr", stream):
            cli._safe_streams()
            print("a → b …")
            stream.flush()
        self.assertEqual(raw.getvalue().decode("utf-8"), "a → b …\n")

    def test_stringio_left_alone(self) -> None:
        with mock.patch.object(sys, "stdout", io.StringIO()):
            cli._safe_streams()


if __name__ == "__main__":
    unittest.main()
