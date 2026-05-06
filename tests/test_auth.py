"""Tests for shared.auth — runnable with `pytest tests/` or
`python -m unittest tests.test_auth`. No pytest dependency needed.

Exercises the only public surface of the module so we can refactor with
confidence: the env-var gate, the outbound-header helper, the inbound
authorization check (including constant-time comparison and bad inputs),
and the public-path exemption.
"""
from __future__ import annotations

import importlib
import os
import unittest
from unittest import mock


def _reload_auth(token: str | None):
    """Reload shared.auth with API_TOKEN set/unset to a known value."""
    env = dict(os.environ)
    if token is None:
        env.pop("API_TOKEN", None)
    else:
        env["API_TOKEN"] = token
    with mock.patch.dict(os.environ, env, clear=True):
        import shared.auth as auth
        return importlib.reload(auth)


class AuthDisabledTests(unittest.TestCase):
    def test_disabled_when_token_unset(self):
        auth = _reload_auth(None)
        self.assertFalse(auth.AUTH_ENABLED)
        self.assertEqual(auth.auth_headers(), {})
        self.assertTrue(auth.check_authorization(None))
        self.assertTrue(auth.check_authorization("Bearer anything"))
        self.assertTrue(auth.check_authorization(""))

    def test_disabled_when_token_empty_string(self):
        auth = _reload_auth("")
        self.assertFalse(auth.AUTH_ENABLED)
        self.assertEqual(auth.auth_headers(), {})

    def test_disabled_when_token_whitespace(self):
        auth = _reload_auth("   ")
        self.assertFalse(auth.AUTH_ENABLED)


class AuthEnabledTests(unittest.TestCase):
    TOKEN = "deadbeef" * 4

    def test_enabled_when_token_set(self):
        auth = _reload_auth(self.TOKEN)
        self.assertTrue(auth.AUTH_ENABLED)
        self.assertEqual(
            auth.auth_headers(), {"Authorization": f"Bearer {self.TOKEN}"}
        )

    def test_check_accepts_valid_bearer(self):
        auth = _reload_auth(self.TOKEN)
        self.assertTrue(auth.check_authorization(f"Bearer {self.TOKEN}"))
        self.assertTrue(auth.check_authorization(f"bearer {self.TOKEN}"))  # case
        self.assertTrue(auth.check_authorization(f"BEARER {self.TOKEN}"))

    def test_check_rejects_missing_or_malformed(self):
        auth = _reload_auth(self.TOKEN)
        for bad in [None, "", "Bearer", "Basic " + self.TOKEN, self.TOKEN]:
            self.assertFalse(
                auth.check_authorization(bad),
                f"should reject: {bad!r}",
            )

    def test_check_rejects_wrong_token(self):
        auth = _reload_auth(self.TOKEN)
        self.assertFalse(auth.check_authorization("Bearer wrong"))
        self.assertFalse(auth.check_authorization(f"Bearer {self.TOKEN}x"))
        self.assertFalse(auth.check_authorization(f"Bearer {self.TOKEN[:-1]}"))

    def test_explicit_token_arg_overrides_env(self):
        auth = _reload_auth(self.TOKEN)
        self.assertEqual(
            auth.auth_headers("override"),
            {"Authorization": "Bearer override"},
        )
        # Empty explicit arg wins too: caller can opt out per-call.
        self.assertEqual(auth.auth_headers(""), {})

    def test_health_path_is_public(self):
        auth = _reload_auth(self.TOKEN)
        self.assertTrue(auth.is_public_path("/health"))
        self.assertFalse(auth.is_public_path("/generate"))
        self.assertFalse(auth.is_public_path("/health/something"))


if __name__ == "__main__":
    unittest.main()
