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
        # v1.1.26: chat default upgraded from 1B → 3B so the
        # image-prompt rewrite pipeline can actually interpret intent.
        self.assertEqual(cfg.bootstrap_model, "llama3.2:3b")
        self.assertEqual(cfg.bootstrap_ollama_url, "http://localhost:11434")
        # Default mirror_base is None → caller should fall back to
        # coordinator_url. Verified separately below.
        self.assertIsNone(cfg.bootstrap_mirror_base_url)

    def test_legacy_chat_model_auto_migrates(self):
        # v1.1.26: existing installs that have llama3.2:1b in their
        # config.json get auto-upgraded on first load to the new
        # default, and the migration is persisted so it doesn't run
        # again. Catches the "operator has to hand-edit config.json
        # to get the upgrade" footgun.
        import json
        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False,
        ) as f:
            json.dump(
                {
                    "coordinator_url": "https://x",
                    "polling_interval_seconds": 5,
                    "earnings_print_minutes": 10,
                    "idle": {
                        "min_input_idle_seconds": 60,
                        "max_cpu_percent": 30,
                        "cpu_sample_seconds": 2,
                    },
                    "bootstrap": {"model": "llama3.2:1b"},
                },
                f,
            )
            cfg_path = Path(f.name)
        try:
            cfg = agent.Config.load(cfg_path)
            self.assertEqual(cfg.bootstrap_model, "llama3.2:3b")
            # Migration persisted to disk — next load reads the new
            # value without needing the migrator.
            with open(cfg_path) as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["bootstrap"]["model"], "llama3.2:3b")
        finally:
            cfg_path.unlink()

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
            bootstrap_image_enabled=True,
            bootstrap_image_model="sd1.5",
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


class SearchInferenceTests(unittest.TestCase):
    """Cover the worker-side search helpers in isolation: DDG is mocked,
    Ollama is mocked, so these tests verify the assembly logic without
    touching the network or a real LLM."""

    def test_domain_strips_www_and_scheme(self):
        self.assertEqual(agent._domain_of("https://www.example.com/foo"), "example.com")
        self.assertEqual(agent._domain_of("http://news.test/path?x=1"), "news.test")
        # Bad input falls through to the raw string rather than raising.
        self.assertEqual(agent._domain_of(""), "")

    def test_build_system_prompt_includes_citations_and_urls(self):
        results = [
            {"title": "Title A", "href": "https://a.com/x", "body": "Snippet A"},
            {"title": "Title B", "href": "https://b.com/y", "body": "Snippet B"},
        ]
        out = agent._build_search_system_prompt("my query", results, "fast")
        self.assertIn("User query: my query", out)
        self.assertIn("[1] Title A", out)
        self.assertIn("URL: https://a.com/x", out)
        self.assertIn("Snippet A", out)
        self.assertIn("[2] Title B", out)
        # Fast mode must NOT embed page bodies (we don't even fetch them),
        # so the "Body:" key should be absent.
        self.assertNotIn("Body:", out)
        # Citation instructions must survive future prompt edits.
        self.assertIn("Cite sources", out)

    def test_build_system_prompt_comprehensive_includes_body(self):
        results = [
            {"title": "T", "href": "https://x.test/p",
             "body": "snip", "_extracted": "extracted body text" * 10},
        ]
        out = agent._build_search_system_prompt("q", results, "comprehensive")
        self.assertIn("Body:", out)
        self.assertIn("extracted body text", out)
        # Body must be capped — passing the entire extracted text would
        # blow the LLM context window.
        body_line = [
            ln for ln in out.split("\n") if ln.strip().startswith("Body:")
        ][0]
        # Cap is SEARCH_PAGE_CHAR_CAP plus the "    Body: " prefix.
        self.assertLessEqual(len(body_line), agent.SEARCH_PAGE_CHAR_CAP + 16)

    def test_run_search_inference_happy_path(self):
        fake_results = [
            {"title": "A", "href": "https://a.com/x", "body": "snip A"},
            {"title": "B", "href": "https://b.com/y", "body": "snip B"},
        ]
        fake_summary = {
            "text": "Answer mentions [1] and [2].",
            "prompt_tokens": 50, "completion_tokens": 10, "model": "test",
        }
        log = mock.MagicMock()
        job = {"job_id": "j1", "search": {"mode": "fast"}}
        with mock.patch.object(agent, "_ddg_search", return_value=fake_results), \
             mock.patch.object(agent, "run_inference", return_value=fake_summary):
            out = agent.run_search_inference(
                prompt="what happened?", job=job, model="m", log=log,
            )
        self.assertEqual(out["text"], fake_summary["text"])
        self.assertEqual(out["model"], "test")
        self.assertEqual(len(out["sources"]), 2)
        # Source numbers match the [1][2] order the system prompt uses.
        self.assertEqual(out["sources"][0]["n"], 1)
        self.assertEqual(out["sources"][0]["url"], "https://a.com/x")
        self.assertEqual(out["sources"][0]["domain"], "a.com")
        self.assertEqual(out["sources"][1]["n"], 2)
        self.assertEqual(out["sources"][1]["domain"], "b.com")

    def test_run_search_inference_zero_results_raises(self):
        log = mock.MagicMock()
        with mock.patch.object(agent, "_ddg_search", return_value=[]):
            with self.assertRaises(RuntimeError) as cm:
                agent.run_search_inference(
                    prompt="x", job={"job_id": "j"}, model=None, log=log,
                )
        # User-facing string — the error bubbles up to /jobs/complete
        # and the UI shows it verbatim.
        self.assertIn("no search results", str(cm.exception))

    def test_run_search_inference_ddg_failure_wraps(self):
        log = mock.MagicMock()
        with mock.patch.object(
            agent, "_ddg_search", side_effect=Exception("boom"),
        ):
            with self.assertRaises(RuntimeError) as cm:
                agent.run_search_inference(
                    prompt="x", job={"job_id": "j"}, model=None, log=log,
                )
        # Wrapped so the worker error log gets a clear "web search
        # failed" prefix instead of bare exception classes.
        self.assertIn("web search failed", str(cm.exception))

    def test_ordered_queues_includes_search(self):
        # When the agent advertises chat + search, /jobs/next should
        # BLPOP both — otherwise search jobs would sit on the queue
        # indefinitely.
        order = agent._ordered_queues(["chat", "search"], last_tool=None)
        self.assertIn("search", order)
        self.assertEqual(order[0], "chat")  # chat first by default
        # Last-served preference flips: a chain of search jobs keeps
        # the search dependency cache warm in front.
        order = agent._ordered_queues(["chat", "search"], last_tool="search")
        self.assertEqual(order[0], "search")


if __name__ == "__main__":
    unittest.main()
