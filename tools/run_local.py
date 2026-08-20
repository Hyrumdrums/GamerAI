"""Local dev harness: run coordinator + client + a mock worker in one
process, no Docker/Redis/Ollama install required.

    .venv/bin/python tools/run_local.py

Everything lives in the venv already used for tests (fakeredis, uvicorn,
httpx are all test/runtime deps already). One in-memory fakeredis
instance is shared by all three components by monkeypatching the two
places that construct a real Redis client — coordinator.redis_client and
worker.worker.connect_redis — before those modules are imported.

Not a Docker replacement for anything topology-sensitive (multi-worker
scaling, real Redis persistence, TLS/Caddy). It's for "does the page
render / does a click round-trip actually work" iteration.
"""
import os
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CLIENT_PORT = 8080
COORDINATOR_PORT = 8901
DEV_TOKEN = "dev-local-token"

# Persisted across runs (data/ is already gitignored) so restarting the
# script doesn't throw away accounts you created while clicking around.
data_dir = REPO_ROOT / "data"
data_dir.mkdir(exist_ok=True)
os.environ.setdefault("DB_PATH", str(data_dir / "gamerai-dev.db"))
os.environ.setdefault("COORDINATOR_URL", f"http://127.0.0.1:{COORDINATOR_PORT}")
os.environ.setdefault("MOCK_INFERENCE", "true")
os.environ.setdefault("API_TOKEN", DEV_TOKEN)

import fakeredis  # noqa: E402

shared_fake = fakeredis.FakeStrictRedis(decode_responses=True)

import coordinator.redis_client as redis_client  # noqa: E402
redis_client.get_client = lambda: shared_fake

import uvicorn  # noqa: E402
from coordinator.main import app as coordinator_app  # noqa: E402
from client.app import app as client_app  # noqa: E402

# worker.py talks to Redis directly (BLPOP on the job queue) rather than
# over HTTP, so it needs the same fake client wired in the same way —
# import it after MOCK_INFERENCE/API_TOKEN are set (it reads both at
# module import time) and patch its connection factory before main()
# runs.
import worker.worker as worker_mod  # noqa: E402
worker_mod.connect_redis = lambda: shared_fake


def _serve(app, port):
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main():
    threading.Thread(target=_serve, args=(coordinator_app, COORDINATOR_PORT), daemon=True).start()
    threading.Thread(target=_serve, args=(client_app, CLIENT_PORT), daemon=True).start()
    threading.Thread(target=worker_mod.main, daemon=True).start()
    time.sleep(1.5)  # let the admin/guest seed + worker registration land before printing the banner

    print(f"""
GamerAI dev server is up:

  Web UI:      http://127.0.0.1:{CLIENT_PORT}
  Coordinator: http://127.0.0.1:{COORDINATOR_PORT}

  Admin sign-in: /login -> "Sign in with a bearer token instead" -> {DEV_TOKEN}
  Or create a normal account: click "Try the demo" (no login) or
  "Download the agent" -> agent's --signup flow, or POST /signup directly:
    curl -X POST http://127.0.0.1:{COORDINATOR_PORT}/signup \\
      -H 'Content-Type: application/json' \\
      -d '{{"username":"me","password":"changeme123","email":"me@example.com","tos_accepted":true}}'

  Mock inference: replies are canned lorem-ipsum text, not a real model.
  Data persists in ./data/gamerai-dev.db between runs — delete it for a clean slate.

  Ctrl+C to stop.
""")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopping.")


if __name__ == "__main__":
    main()
