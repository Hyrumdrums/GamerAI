#!/usr/bin/env python3
"""Sign a built agent.exe with the Ed25519 release key.

Used by the windows-agent build workflow. Reads the base64 private seed
from the ``AGENT_SIGNING_KEY`` environment variable (the GitHub Actions
secret), signs the raw bytes of the file given as argv[1], and writes a
base64 Ed25519 signature to ``<file>.sig``.

    AGENT_SIGNING_KEY=<b64 seed> python tools/sign_agent.py dist/agent.exe
    # -> writes dist/agent.exe.sig

Exit codes:
  0  signed OK
  2  AGENT_SIGNING_KEY missing/blank (caller decides if that's fatal)
  3  bad key or unreadable input

The agent verifies this signature against the embedded UPDATE_PUBLIC_KEY
before applying any self-update (see windows-agent/agent.py).
"""
import base64
import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: sign_agent.py <path-to-file>\n")
        return 3
    seed_b64 = (os.environ.get("AGENT_SIGNING_KEY") or "").strip()
    if not seed_b64:
        sys.stderr.write("AGENT_SIGNING_KEY is empty\n")
        return 2
    try:
        priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64))
    except Exception as e:  # noqa: BLE001 — surface any decode/parse failure
        sys.stderr.write(f"could not load signing key: {e}\n")
        return 3
    target = argv[1]
    try:
        with open(target, "rb") as f:
            payload = f.read()
    except OSError as e:
        sys.stderr.write(f"could not read {target}: {e}\n")
        return 3
    sig_b64 = base64.b64encode(priv.sign(payload)).decode("ascii")
    out_path = target + ".sig"
    with open(out_path, "w", encoding="ascii", newline="") as f:
        f.write(sig_b64)
    sys.stderr.write(f"wrote {out_path} ({len(sig_b64)} b64 chars)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
