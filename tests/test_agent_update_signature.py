"""Tests for signed self-updates in the Windows agent (H2 hardening).

Covers the pure Ed25519 verifier, the fail-closed authenticity gate, and
an end-to-end round-trip through the real CI signer (tools/sign_agent.py)
to prove the signer and verifier agree on format.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "windows-agent"
sys.path.insert(0, str(AGENT_DIR))
import agent  # noqa: E402  (path injection above is intentional)


def _make_keypair() -> tuple[str, str]:
    """Return (public_key_b64, private_seed_b64)."""
    priv = Ed25519PrivateKey.generate()
    seed = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        base64.b64encode(pub).decode("ascii"),
        base64.b64encode(seed).decode("ascii"),
    )


def _sign(seed_b64: str, message: bytes) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64))
    return base64.b64encode(priv.sign(message)).decode("ascii")


class Ed25519VerifyTests(unittest.TestCase):
    def setUp(self):
        self.pub, self.seed = _make_keypair()
        self.msg = b"MZ\x90\x00 ...pretend this is agent.exe..."

    def test_valid_signature_accepts(self):
        sig = _sign(self.seed, self.msg)
        self.assertTrue(agent._verify_ed25519(self.pub, self.msg, sig))

    def test_tampered_message_rejects(self):
        sig = _sign(self.seed, self.msg)
        self.assertFalse(
            agent._verify_ed25519(self.pub, self.msg + b"x", sig)
        )

    def test_wrong_key_rejects(self):
        other_pub, _ = _make_keypair()
        sig = _sign(self.seed, self.msg)
        self.assertFalse(agent._verify_ed25519(other_pub, self.msg, sig))

    def test_garbage_signature_rejects(self):
        self.assertFalse(
            agent._verify_ed25519(self.pub, self.msg, "not-base64-!!")
        )
        self.assertFalse(agent._verify_ed25519(self.pub, self.msg, ""))

    def test_garbage_pubkey_rejects(self):
        sig = _sign(self.seed, self.msg)
        self.assertFalse(agent._verify_ed25519("xx", self.msg, sig))


class UpdateAuthenticityGateTests(unittest.TestCase):
    """The gate must FAIL CLOSED whenever a public key is configured."""

    def setUp(self):
        self.pub, self.seed = _make_keypair()
        self.tmp = Path(tempfile.mkdtemp(prefix="gai-sig-test-"))
        self.staged = self.tmp / "agent.exe"
        self.payload = b"MZ binary payload bytes"
        self.staged.write_bytes(self.payload)
        self.log = mock.Mock()

    def _patch_sig_response(self, *, status: int, text: str):
        """Patch agent.httpx.Client so GET returns a canned sig sidecar."""
        resp = mock.Mock()
        resp.status_code = status
        resp.text = text
        client = mock.MagicMock()
        client.__enter__.return_value.get.return_value = resp
        return mock.patch.object(agent.httpx, "Client", return_value=client)

    def test_unconfigured_key_passes_with_warning(self):
        # No embedded key → SHA-only legacy behavior, returns True.
        with mock.patch.object(agent, "UPDATE_PUBLIC_KEY", ""):
            self.assertTrue(
                agent._verify_update_authenticity(
                    self.staged, "https://x.example", self.log
                )
            )
        self.assertTrue(self.log.warning.called)

    def test_configured_key_valid_signature_passes(self):
        sig = _sign(self.seed, self.payload)
        with mock.patch.object(agent, "UPDATE_PUBLIC_KEY", self.pub), \
             self._patch_sig_response(status=200, text=sig + "\n"):
            self.assertTrue(
                agent._verify_update_authenticity(
                    self.staged, "https://x.example", self.log
                )
            )

    def test_configured_key_bad_signature_rejects(self):
        other_pub, other_seed = _make_keypair()
        forged = _sign(other_seed, self.payload)  # signed by the wrong key
        with mock.patch.object(agent, "UPDATE_PUBLIC_KEY", self.pub), \
             self._patch_sig_response(status=200, text=forged):
            self.assertFalse(
                agent._verify_update_authenticity(
                    self.staged, "https://x.example", self.log
                )
            )

    def test_configured_key_missing_sidecar_rejects(self):
        # 404 on the .sig → fail closed (this is the core anti-tamper).
        with mock.patch.object(agent, "UPDATE_PUBLIC_KEY", self.pub), \
             self._patch_sig_response(status=404, text=""):
            self.assertFalse(
                agent._verify_update_authenticity(
                    self.staged, "https://x.example", self.log
                )
            )

    def test_configured_key_fetch_error_rejects(self):
        client = mock.MagicMock()
        client.__enter__.return_value.get.side_effect = Exception("net down")
        with mock.patch.object(agent, "UPDATE_PUBLIC_KEY", self.pub), \
             mock.patch.object(agent.httpx, "Client", return_value=client):
            self.assertFalse(
                agent._verify_update_authenticity(
                    self.staged, "https://x.example", self.log
                )
            )

    def test_signature_over_different_bytes_rejects(self):
        # Valid signature, but over content other than the staged file.
        sig = _sign(self.seed, b"some other build")
        with mock.patch.object(agent, "UPDATE_PUBLIC_KEY", self.pub), \
             self._patch_sig_response(status=200, text=sig):
            self.assertFalse(
                agent._verify_update_authenticity(
                    self.staged, "https://x.example", self.log
                )
            )


class CiSignerRoundTripTests(unittest.TestCase):
    """The real tools/sign_agent.py output must verify under the agent."""

    def test_sign_agent_then_agent_verifies(self):
        pub, seed = _make_keypair()
        tmp = Path(tempfile.mkdtemp(prefix="gai-sign-rt-"))
        target = tmp / "agent.exe"
        payload = b"MZ end-to-end signer/verifier agreement"
        target.write_bytes(payload)

        env = dict(os.environ, AGENT_SIGNING_KEY=seed)
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "sign_agent.py"),
             str(target)],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sig_b64 = (tmp / "agent.exe.sig").read_text().strip()
        self.assertTrue(agent._verify_ed25519(pub, payload, sig_b64))

    def test_sign_agent_missing_key_exits_2(self):
        tmp = Path(tempfile.mkdtemp(prefix="gai-sign-nokey-"))
        target = tmp / "agent.exe"
        target.write_bytes(b"MZ")
        env = dict(os.environ)
        env.pop("AGENT_SIGNING_KEY", None)
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "sign_agent.py"),
             str(target)],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)


if __name__ == "__main__":
    unittest.main()
