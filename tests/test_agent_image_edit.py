"""Tests for image alteration (img2img) in run_image_inference().

Can't exercise the real sd.exe binary from Linux — these tests
monkeypatch IS_WINDOWS + the sd.exe/model path lookups and replace
subprocess.run with a fake that captures the argv it was called with
(and writes a stub output PNG, mimicking what a real sd.exe run would
leave behind), so the argv-construction logic — the part hand-derived
from upstream's CLI source rather than verified against a real
Windows box — gets real regression coverage.

Run with ``python -m unittest tests.test_agent_image_edit``.
"""
from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parent.parent / "windows-agent"
sys.path.insert(0, str(AGENT_DIR))
import agent  # noqa: E402  (path injection above is intentional)

_FAKE_INIT_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"not a real png, just bytes for the test"
_FAKE_INIT_IMAGE_B64 = base64.b64encode(_FAKE_INIT_IMAGE_BYTES).decode("ascii")


class RunImageInferenceEditTests(unittest.TestCase):
    def _cfg(self, **overrides):
        defaults = dict(
            coordinator_url="https://example.test",
            polling_interval=5.0,
            earnings_print_seconds=600.0,
            min_input_idle_seconds=60.0,
            max_cpu_percent=30.0,
            cpu_sample_seconds=2.0,
            override_drain=False,
            max_gpu_percent=25.0,
            game_processes=[],
            keep_awake_while_online=True,
            update_enabled=True,
            update_check_interval_hours=6.0,
            bootstrap_enabled=True,
            bootstrap_model="llama3.2:1b",
            bootstrap_ollama_url="http://localhost:11434",
            bootstrap_mirror_base_url=None,
            bootstrap_image_enabled=True,
            bootstrap_image_model="sd1.5",
            bootstrap_tts_enabled=False,
            bootstrap_tts_model="piper:en_us-libritts-high",
            model=None,
            worker_id=None,
            api_token=None,
        )
        defaults.update(overrides)
        return agent.Config(**defaults)

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)

        self._fake_sd_exe = tmp / "sd.exe"
        self._fake_sd_exe.write_bytes(b"not a real binary")
        self._fake_model = tmp / "sd1.5.gguf"
        self._fake_model.write_bytes(b"not a real gguf")

        self._patches = [
            mock.patch.object(agent, "IS_WINDOWS", True),
            mock.patch.object(agent, "sd_binary_path", return_value=self._fake_sd_exe),
            mock.patch.object(agent, "sd_model_path", return_value=self._fake_model),
            mock.patch.object(agent, "sd_install_dir", return_value=tmp),
            mock.patch.object(agent, "load_sd_model_defaults", return_value={
                "default_width": 512, "default_height": 512,
                "default_steps": 8, "default_cfg_scale": 1.5,
                "default_sampler": "lcm",
            }),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmpdir.cleanup)

    def _run_with_fake_subprocess(self, job: dict):
        """Runs run_image_inference with subprocess.run replaced by a
        fake that captures argv and writes a stub PNG to whatever -o
        path it was given (so the out_path.exists()/read_bytes() path
        in run_image_inference succeeds). Returns (result, argv,
        init_image_path_at_call_time)."""
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            out_idx = argv.index("-o") + 1
            Path(argv[out_idx]).write_bytes(b"\x89PNG\r\n\x1a\nfake-output")
            if "-i" in argv:
                init_idx = argv.index("-i") + 1
                captured["init_image_path"] = Path(argv[init_idx])
                captured["init_image_existed_at_call"] = Path(argv[init_idx]).exists()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(agent.subprocess, "run", side_effect=fake_run):
            result = agent.run_image_inference("a cat", job, self._cfg(), mock.MagicMock())
        return result, captured

    def test_txt2img_unaffected_when_no_init_image(self):
        (b64, model_used), captured = self._run_with_fake_subprocess({"image": {}})
        argv = captured["argv"]
        self.assertNotIn("-i", argv)
        self.assertNotIn("--strength", argv)
        self.assertEqual(model_used, "sd1.5")
        self.assertTrue(b64)  # real base64 output, not the mock branch

    def test_init_image_written_and_passed_as_argv(self):
        job = {"image": {"init_image_b64": _FAKE_INIT_IMAGE_B64}}
        _result, captured = self._run_with_fake_subprocess(job)
        argv = captured["argv"]
        self.assertIn("-i", argv)
        self.assertTrue(captured["init_image_existed_at_call"])
        # Cleaned up after the call — no leaked temp file.
        self.assertFalse(captured["init_image_path"].exists())

    def test_strength_passed_when_provided(self):
        job = {"image": {"init_image_b64": _FAKE_INIT_IMAGE_B64, "strength": 0.35}}
        _result, captured = self._run_with_fake_subprocess(job)
        argv = captured["argv"]
        self.assertIn("--strength", argv)
        self.assertEqual(argv[argv.index("--strength") + 1], "0.35")

    def test_strength_omitted_when_not_provided(self):
        # -i present, no strength — sd.exe's own default should apply,
        # not a value invented on the agent side.
        job = {"image": {"init_image_b64": _FAKE_INIT_IMAGE_B64}}
        _result, captured = self._run_with_fake_subprocess(job)
        self.assertIn("-i", captured["argv"])
        self.assertNotIn("--strength", captured["argv"])

    def test_invalid_base64_raises_runtime_error(self):
        job = {"image": {"init_image_b64": "not-valid-base64!!!"}}
        with self.assertRaises(RuntimeError):
            self._run_with_fake_subprocess(job)

    def test_init_image_cleaned_up_even_on_subprocess_failure(self):
        job = {"image": {"init_image_b64": _FAKE_INIT_IMAGE_B64}}
        captured = {}

        def failing_run(argv, **kwargs):
            captured["argv"] = argv
            if "-i" in argv:
                captured["init_image_path"] = Path(argv[argv.index("-i") + 1])
            return mock.Mock(returncode=1, stdout="", stderr="boom")

        with mock.patch.object(agent.subprocess, "run", side_effect=failing_run):
            with self.assertRaises(RuntimeError):
                agent.run_image_inference("a cat", job, self._cfg(), mock.MagicMock())
        self.assertFalse(captured["init_image_path"].exists())


if __name__ == "__main__":
    unittest.main()
