"""shared.config helpers — Redis connection kwargs, incl. the optional
password wiring (L6 hardening)."""
from __future__ import annotations

import unittest
from unittest import mock

from shared import config


class RedisKwargsTests(unittest.TestCase):
    def test_no_password_anywhere_is_none(self):
        with mock.patch.object(config, "REDIS_URL", "redis://redis:6379/0"), \
             mock.patch.object(config, "REDIS_PASSWORD", ""):
            self.assertIsNone(config.redis_kwargs()["password"])

    def test_env_password_used_when_url_has_none(self):
        with mock.patch.object(config, "REDIS_URL", "redis://redis:6379/0"), \
             mock.patch.object(config, "REDIS_PASSWORD", "s3cret"):
            self.assertEqual(config.redis_kwargs()["password"], "s3cret")

    def test_url_password_takes_precedence_over_env(self):
        with mock.patch.object(
                config, "REDIS_URL", "redis://:urlpw@redis:6379/0"), \
             mock.patch.object(config, "REDIS_PASSWORD", "envpw"):
            self.assertEqual(config.redis_kwargs()["password"], "urlpw")

    def test_host_port_db_parsed(self):
        with mock.patch.object(config, "REDIS_URL", "redis://h:6390/2"), \
             mock.patch.object(config, "REDIS_PASSWORD", ""):
            kw = config.redis_kwargs()
            self.assertEqual(kw["host"], "h")
            self.assertEqual(kw["port"], 6390)
            self.assertEqual(kw["db"], 2)


if __name__ == "__main__":
    unittest.main()
