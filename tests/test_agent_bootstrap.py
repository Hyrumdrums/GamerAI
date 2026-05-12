"""Tests for the Windows agent's first-run bootstrap.

The bootstrap itself only runs on Windows (it installs Ollama and pulls
a model). On Linux we can still exercise: config parsing, slug shape,
non-Windows short-circuit, /api/tags parsing, and the orchestrator's
"don't try if disabled" path.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

AGENT_DIR = Path(__file__).resolve().parent.parent / "windows-agent"
sys.path.insert(0, str(AGENT_DIR))
import agent  # noqa: E402  (path injection above is intentional)


class ModelSlugTests(unittest.TestCase):
    def test_colon_to_dash(self):
        self.assertEqual(agent._model_slug("llama3.2:1b"), "llama3.2-1b")

    def test_slash_to_dash(self):
        self.assertEqual(agent._model_slug("vendor/model:tag"), "vendor-model-tag")

    def test_no_separators_unchanged(self):
        self.assertEqual(agent._model_slug("mistral"), "mistral")


class FormatEtaTests(unittest.TestCase):
    def test_sub_minute(self):
        self.assertEqual(agent._format_eta(0), "0s")
        self.assertEqual(agent._format_eta(45), "45s")

    def test_minutes(self):
        self.assertEqual(agent._format_eta(60), "1m 00s")
        self.assertEqual(agent._format_eta(125), "2m 05s")

    def test_hours(self):
        self.assertEqual(agent._format_eta(3725), "1h 02m")

    def test_negative_or_nan(self):
        self.assertEqual(agent._format_eta(-1), "?")
        self.assertEqual(agent._format_eta(float("nan")), "?")


class ConfigBootstrapTests(unittest.TestCase):
    def test_defaults_present(self):
        cfg = agent.Config.load(None)
        self.assertTrue(cfg.bootstrap_enabled)
        self.assertEqual(cfg.bootstrap_model, "llama3.2:1b")
        self.assertEqual(cfg.bootstrap_ollama_url, "http://localhost:11434")
        # Default mirror_base is None → caller should fall back to
        # coordinator_url. Verified separately below.
        self.assertIsNone(cfg.bootstrap_mirror_base_url)

    def test_user_overrides_merge(self, tmp_path=None):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(
                {
                    "bootstrap": {
                        "enabled": False,
                        "model": "mistral:7b",
                        "mirror_base_url": "https://mirror.example.com",
                    }
                },
                f,
            )
            path = Path(f.name)
        try:
            cfg = agent.Config.load(path)
            self.assertFalse(cfg.bootstrap_enabled)
            self.assertEqual(cfg.bootstrap_model, "mistral:7b")
            self.assertEqual(
                cfg.bootstrap_mirror_base_url, "https://mirror.example.com"
            )
            # Untouched bootstrap keys keep their defaults.
            self.assertEqual(cfg.bootstrap_ollama_url, "http://localhost:11434")
        finally:
            path.unlink(missing_ok=True)


class BootstrapInferenceShortCircuitTests(unittest.TestCase):
    """The orchestrator should bail quickly without touching the
    network when it has no work to do."""

    def _cfg(self, **overrides):
        defaults = dict(
            coordinator_url="https://example.test",
            polling_interval=5.0,
            earnings_print_seconds=600.0,
            min_input_idle_seconds=60.0,
            max_cpu_percent=30.0,
            cpu_sample_seconds=2.0,
            override_drain=False,
            keep_awake_while_online=True,
            update_enabled=True,
            update_check_interval_hours=6.0,
            bootstrap_enabled=True,
            bootstrap_model="llama3.2:1b",
            bootstrap_ollama_url="http://localhost:11434",
            bootstrap_mirror_base_url=None,
            model=None,
            worker_id=None,
            api_token=None,
        )
        defaults.update(overrides)
        return agent.Config(**defaults)

    def test_disabled_returns_none(self):
        cfg = self._cfg(bootstrap_enabled=False)
        log = mock.MagicMock()
        with mock.patch.object(agent, "_ollama_responding") as probe:
            self.assertIsNone(agent.bootstrap_inference(cfg, log))
            probe.assert_not_called()

    def test_non_windows_returns_none(self):
        cfg = self._cfg()
        log = mock.MagicMock()
        with mock.patch.object(agent, "IS_WINDOWS", False), \
                mock.patch.object(agent, "_ollama_responding") as probe:
            self.assertIsNone(agent.bootstrap_inference(cfg, log))
            probe.assert_not_called()


class ModelPresentTests(unittest.TestCase):
    """`/api/tags` parsing must distinguish present from missing."""

    def _fake_client(self, payload, status=200):
        class _Resp:
            status_code = status

            def raise_for_status(self):
                if status >= 400:
                    raise httpx.HTTPError("nope")

            def json(self):
                return payload

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **kw):
                return _Resp()

        return _Client

    def test_present(self):
        log = mock.MagicMock()
        client = self._fake_client(
            {"models": [{"name": "llama3.2:1b"}, {"name": "mistral:7b"}]}
        )
        with mock.patch.object(agent.httpx, "Client", client):
            self.assertTrue(
                agent._model_present("http://localhost:11434", "llama3.2:1b", log)
            )

    def test_absent(self):
        log = mock.MagicMock()
        client = self._fake_client({"models": [{"name": "mistral:7b"}]})
        with mock.patch.object(agent.httpx, "Client", client):
            self.assertFalse(
                agent._model_present("http://localhost:11434", "llama3.2:1b", log)
            )

    def test_api_error_returns_false(self):
        log = mock.MagicMock()
        client = self._fake_client({}, status=500)
        with mock.patch.object(agent.httpx, "Client", client):
            self.assertFalse(
                agent._model_present("http://localhost:11434", "llama3.2:1b", log)
            )


if __name__ == "__main__":
    unittest.main()
