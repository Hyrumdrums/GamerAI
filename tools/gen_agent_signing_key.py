#!/usr/bin/env python3
"""Generate the Ed25519 keypair for signing GamerAI Windows-agent updates.

Run this ONCE per deployment, locally (never on the VPS). It prints two
values:

  * the PUBLIC key  — paste into ``UPDATE_PUBLIC_KEY`` in
    windows-agent/agent.py and commit it.
  * the PRIVATE seed — add as the repo secret ``AGENT_SIGNING_KEY``
    (Settings → Secrets and variables → Actions). CI uses it to sign
    each published agent.exe. It must NEVER be committed or copied to
    the VPS — that's the whole point: the download host can't forge an
    update because it doesn't hold this.

Once both are in place, the windows-agent build workflow publishes
agent.exe.sig alongside agent.exe, and every agent on a key-embedded
build fail-closed-verifies the signature before swapping binaries.

See docs/OPERATOR.md "Agent update signing" for the full runbook.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
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
    pub_b64 = base64.b64encode(pub).decode("ascii")
    seed_b64 = base64.b64encode(seed).decode("ascii")

    print("=" * 70)
    print("GamerAI agent update signing key — generated")
    print("=" * 70)
    print()
    print("1) PUBLIC KEY  → paste into UPDATE_PUBLIC_KEY in")
    print("   windows-agent/agent.py, then commit:")
    print()
    print(f'   UPDATE_PUBLIC_KEY = "{pub_b64}"')
    print()
    print("2) PRIVATE SEED → add as the GitHub Actions repo secret")
    print("   AGENT_SIGNING_KEY (do NOT commit, do NOT put on the VPS):")
    print()
    print(f"   {seed_b64}")
    print()
    print("Store the private seed in your password manager too — losing")
    print("it means you can't publish further signed updates (rotate by")
    print("generating a new pair and shipping it via the SHA path once).")
    print("=" * 70)


if __name__ == "__main__":
    main()
