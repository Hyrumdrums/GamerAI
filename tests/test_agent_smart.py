"""Tests for the Windows agent's smart-mode plumbing.

Process management (launching llama-server / rpc-server) is Windows +
hardware territory; what we CAN pin down on Linux CI is everything
around it: config parsing, the queue-order changes, the exact llama.cpp
command lines we emit, the SSE protocol parsing, and a full
run_smart_inference round-trip against a stub OpenAI-compatible server.
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "windows-agent"
sys.path.insert(0, str(AGENT_DIR))
import agent  # noqa: E402  (path injection above is intentional)

import logging  # noqa: E402

_LOG = logging.getLogger("test-smart")


class SmartConfigTests(unittest.TestCase):
    def test_defaults_off_and_sane(self):
        cfg = agent.Config.load(None)
        self.assertFalse(cfg.smart_enabled)
        self.assertEqual(cfg.smart_role, "head")
        self.assertEqual(cfg.smart_model, "qwen2.5:14b")
        self.assertEqual(cfg.smart_rpc_peers, [])
        self.assertEqual(cfg.smart_rpc_listen_port, 50052)
        self.assertEqual(cfg.smart_llama_server_port, 8092)
        self.assertIsNone(cfg.smart_endpoint)
        self.assertEqual(cfg.smart_context_length, 8192)

    def test_user_config_round_trips(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False,
        ) as f:
            json.dump({
                "smart": {
                    "enabled": True,
                    "role": "Backend",  # case-insensitive
                    "rpc_peers": ["192.168.1.42:50052", " "],
                    "tensor_split": "5,8",
                    "endpoint": "http://127.0.0.1:9999/",
                },
            }, f)
            path = Path(f.name)
        cfg = agent.Config.load(path)
        self.assertTrue(cfg.smart_enabled)
        self.assertEqual(cfg.smart_role, "backend")
        # Blank peer entries are dropped, real ones kept.
        self.assertEqual(cfg.smart_rpc_peers, ["192.168.1.42:50052"])
        self.assertEqual(cfg.smart_tensor_split, "5,8")
        # Endpoint is normalized without the trailing slash.
        self.assertEqual(cfg.smart_endpoint, "http://127.0.0.1:9999")

    def test_default_release_urls(self):
        self.assertEqual(
            agent._smart_default_llama_zip_url("b9610"),
            "https://github.com/ggml-org/llama.cpp/releases/download/"
            "b9610/llama-b9610-bin-win-cuda-12.4-x64.zip",
        )
        self.assertIn(
            "cudart-llama-bin-win-cuda-12.4-x64.zip",
            agent._smart_default_cudart_zip_url("b9610"),
        )

    def test_default_gguf_known_for_default_model(self):
        cfg = agent.Config.load(None)
        self.assertIn(cfg.smart_model, agent._SMART_GGUF_URLS)

    def test_builtin_gguf_sources_cover_the_test_models(self):
        # The "test with a smaller model" doc promises these names
        # work with no gguf_url override.
        for name in ("qwen2.5:14b", "qwen2.5:7b", "llama3.2:3b"):
            self.assertIn(name, agent._SMART_GGUF_URLS)
            self.assertTrue(
                agent._SMART_GGUF_URLS[name].startswith("https://"),
            )


class SmartConsoleCommandTests(unittest.TestCase):
    """The temporary `smart` stdin command: parse + persist helpers that
    let an operator enable smart mode from a machine's console window."""

    def test_parse_host_port_valid_and_invalid(self):
        self.assertEqual(
            agent._parse_host_port("192.168.1.50:50052", 50052),
            ("192.168.1.50", 50052),
        )
        # Bare host falls back to the default port.
        self.assertEqual(
            agent._parse_host_port("10.0.0.5", 50052), ("10.0.0.5", 50052),
        )
        self.assertEqual(
            agent._parse_host_port("0.0.0.0:50053", 50052), ("0.0.0.0", 50053),
        )
        for bad in ("bad:port", "host:0", "host:70000", "  ", ""):
            self.assertIsNone(
                agent._parse_host_port(bad, 50052), f"{bad!r} should reject",
            )

    def test_apply_smart_config_merges_and_preserves(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        path = d / "config.json"
        path.write_text(json.dumps({
            "coordinator_url": "http://x:8000",
            "smart": {"llama_release": "b9610", "enabled": False},
        }))
        log = logging.getLogger("test-smart-cmd")

        self.assertTrue(agent._apply_smart_config(path, {
            "enabled": True, "role": "backend",
            "rpc_listen_host": "0.0.0.0", "rpc_listen_port": 50052,
        }, log))
        data = json.loads(path.read_text())
        # Unrelated top-level + sibling smart keys survive the write.
        self.assertEqual(data["coordinator_url"], "http://x:8000")
        self.assertEqual(data["smart"]["llama_release"], "b9610")
        self.assertEqual(data["smart"]["role"], "backend")
        self.assertTrue(data["smart"]["enabled"])

        # A second write (head) still preserves the release pin and the
        # agent loads exactly what we persisted.
        self.assertTrue(agent._apply_smart_config(path, {
            "enabled": True, "role": "head",
            "rpc_peers": ["192.168.1.50:50052"], "model": "qwen2.5:7b",
        }, log))
        cfg = agent.Config.load(path)
        self.assertTrue(cfg.smart_enabled)
        self.assertEqual(cfg.smart_role, "head")
        self.assertEqual(cfg.smart_model, "qwen2.5:7b")
        self.assertEqual(cfg.smart_rpc_peers, ["192.168.1.50:50052"])
        self.assertEqual(cfg.smart_llama_release, "b9610")

    def test_apply_smart_config_creates_missing_file(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "config.json"
        log = logging.getLogger("test-smart-cmd")
        self.assertTrue(
            agent._apply_smart_config(path, {"enabled": True, "role": "backend"}, log),
        )
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text())["smart"]["role"], "backend")

    def test_relaunch_in_place_noop_off_windows(self):
        # On non-Windows / non-frozen dev builds there's nothing to
        # relaunch — the caller falls back to "restart to apply".
        if agent.IS_WINDOWS and agent._agent_exe_path() is not None:
            self.skipTest("frozen Windows build — would actually relaunch")
        self.assertFalse(
            agent._relaunch_in_place(logging.getLogger("test-smart-cmd"), []),
        )

    def test_relaunch_batch_is_ascii_clean(self):
        # Regression for the v1.3.0 crash: a .bat is interpreted in the
        # console's OEM code page, so its content MUST be pure ASCII. A
        # stray em-dash made write_text(encoding="ascii") raise
        # UnicodeEncodeError and killed the stdin thread. Force the
        # Windows write path on Linux (the cmd.exe spawn then fails, so
        # the call returns False, but the .bat is written first — that's
        # what we inspect).
        import tempfile
        from unittest import mock
        exe = Path(tempfile.mkdtemp()) / "agent.exe"
        exe.write_bytes(b"MZ")
        with mock.patch.object(agent, "IS_WINDOWS", True), \
                mock.patch.object(agent, "_agent_exe_path", lambda: exe):
            agent._relaunch_in_place(logging.getLogger("test-smart-cmd"), ["--tray"])
        bat = exe.with_name("smart-relaunch.bat")
        self.assertTrue(bat.exists(), "relaunch batch was not written")
        raw = bat.read_bytes()
        raw.decode("ascii")  # must not raise
        self.assertIn(b":waitloop", raw)
        self.assertIn(b"EncodedCommand", raw)


class OrderedQueuesSmartTests(unittest.TestCase):
    def test_smart_head_polls_only_smart_queue(self):
        self.assertEqual(
            agent._ordered_queues(["chat:smart", "tts"], None),
            ["chat:smart"],
        )

    def test_smart_backend_polls_nothing(self):
        self.assertEqual(agent._ordered_queues([], None), [])
        self.assertEqual(agent._ordered_queues(["tts"], None), [])

    def test_legacy_behavior_preserved(self):
        self.assertEqual(agent._ordered_queues(None, None), ["chat"])
        self.assertEqual(
            agent._ordered_queues(["chat", "image", "search", "tts"], "image"),
            ["image", "chat", "search"],
        )

    def test_last_tool_promotion_applies_to_smart(self):
        # Single-queue head: promotion is a no-op but must not crash.
        self.assertEqual(
            agent._ordered_queues(["chat:smart"], "chat:smart"),
            ["chat:smart"],
        )


class BootstrapShortCircuitTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        cfg = agent.Config.load(None)
        self.assertIsNone(agent.bootstrap_smart_runtime(cfg, _LOG))

    def test_unknown_role_returns_none(self):
        cfg = agent.Config.load(None)
        cfg.smart_enabled = True
        cfg.smart_role = "middle"
        self.assertIsNone(agent.bootstrap_smart_runtime(cfg, _LOG))

    def test_managed_launch_is_windows_only(self):
        if agent.IS_WINDOWS:
            self.skipTest("non-Windows short-circuit")
        cfg = agent.Config.load(None)
        cfg.smart_enabled = True
        self.assertIsNone(agent.bootstrap_smart_runtime(cfg, _LOG))


class CommandLineTests(unittest.TestCase):
    """The exact argv we hand llama.cpp — flag typos here would only
    surface at launch time on a contributor's Windows box."""

    def _cfg(self, **overrides):
        cfg = agent.Config.load(None)
        cfg.smart_enabled = True
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def test_backend_command(self):
        cfg = self._cfg(smart_role="backend")
        rt = agent.SmartRuntime(
            cfg, _LOG, bin_dir=Path("/opt/llama"), gguf=None,
        )
        cmd = rt._command()
        self.assertTrue(cmd[0].endswith("rpc-server.exe"))
        self.assertEqual(cmd[cmd.index("-H") + 1], "0.0.0.0")
        self.assertEqual(cmd[cmd.index("-p") + 1], "50052")
        self.assertIn("-c", cmd)  # local tensor cache

    def test_head_command_with_peers_and_split(self):
        cfg = self._cfg(
            smart_role="head",
            smart_rpc_peers=["192.168.1.42:50052", "192.168.1.43:50052"],
            smart_tensor_split="5,8",
            smart_extra_args=["--flash-attn"],
        )
        gguf = Path("/opt/llama/models/qwen2.5-14b.gguf")
        rt = agent.SmartRuntime(
            cfg, _LOG, bin_dir=Path("/opt/llama"), gguf=gguf,
        )
        cmd = rt._command()
        self.assertTrue(cmd[0].endswith("llama-server.exe"))
        self.assertEqual(cmd[cmd.index("-m") + 1], str(gguf))
        # Bound to loopback — only the agent talks to it.
        self.assertEqual(cmd[cmd.index("--host") + 1], "127.0.0.1")
        self.assertEqual(cmd[cmd.index("--port") + 1], "8092")
        self.assertEqual(cmd[cmd.index("-ngl") + 1], "999")
        self.assertEqual(cmd[cmd.index("-c") + 1], "8192")
        self.assertEqual(
            cmd[cmd.index("--rpc") + 1],
            "192.168.1.42:50052,192.168.1.43:50052",
        )
        self.assertEqual(cmd[cmd.index("-ts") + 1], "5,8")
        self.assertEqual(cmd[-1], "--flash-attn")

    def test_head_command_without_peers_omits_rpc_flag(self):
        cfg = self._cfg(smart_role="head")
        rt = agent.SmartRuntime(
            cfg, _LOG, bin_dir=Path("/opt/llama"),
            gguf=Path("/m.gguf"),
        )
        self.assertNotIn("--rpc", rt._command())


class SseParseTests(unittest.TestCase):
    def test_data_lines(self):
        self.assertEqual(agent._parse_sse_data('data: {"a":1}'), '{"a":1}')
        self.assertEqual(agent._parse_sse_data("data: [DONE]"), "[DONE]")
        self.assertEqual(agent._parse_sse_data("data:[DONE]"), "[DONE]")

    def test_non_data_lines_ignored(self):
        self.assertIsNone(agent._parse_sse_data(""))
        self.assertIsNone(agent._parse_sse_data(": keepalive comment"))
        self.assertIsNone(agent._parse_sse_data("event: message"))


class _StubOpenAIHandler(BaseHTTPRequestHandler):
    """Minimal /v1/chat/completions SSE stream, llama-server-shaped:
    delta chunks, then a usage chunk (stream_options.include_usage),
    then [DONE]."""

    tokens = ["Hello", " from", " the", " pipeline"]

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.last_request = body  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())

        for tok in self.tokens:
            send({"choices": [{"delta": {"content": tok}}]})
        send({
            "choices": [],
            "usage": {"prompt_tokens": 21, "completion_tokens": 4},
        })
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args):  # silence test output
        pass


class RunSmartInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _StubOpenAIHandler)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        )
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_streams_text_and_reports_real_usage(self):
        partials: list[str] = []
        out = agent.run_smart_inference(
            "say hi", "qwen2.5:14b", _LOG,
            endpoint=self.endpoint,
            on_partial=partials.append,
        )
        self.assertEqual(out["text"], "Hello from the pipeline")
        self.assertEqual(out["prompt_tokens"], 21)
        self.assertEqual(out["completion_tokens"], 4)
        self.assertEqual(out["model"], "qwen2.5:14b")
        # Final on_partial always fires with the full text.
        self.assertTrue(partials)
        self.assertEqual(partials[-1], "Hello from the pipeline")
        # Bare prompt is wrapped as a single user message; usage was
        # requested on the stream.
        req = self.server.last_request  # type: ignore[attr-defined]
        self.assertEqual(req["messages"], [{"role": "user", "content": "say hi"}])
        self.assertTrue(req["stream"])
        self.assertEqual(req["stream_options"], {"include_usage": True})

    def test_messages_envelope_passes_through(self):
        msgs = [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ]
        agent.run_smart_inference(
            "ignored when messages set", "qwen2.5:14b", _LOG,
            endpoint=self.endpoint, messages=msgs,
        )
        req = self.server.last_request  # type: ignore[attr-defined]
        self.assertEqual(req["messages"], msgs)

    def test_server_error_raises_instead_of_mocking(self):
        # A smart job must never silently return mock text — error out
        # so the coordinator records a failed job with a retry button.
        with self.assertRaises(Exception):
            agent.run_smart_inference(
                "hi", "qwen2.5:14b", _LOG,
                endpoint="http://127.0.0.1:9",  # nothing listens here
            )


if __name__ == "__main__":
    unittest.main()
