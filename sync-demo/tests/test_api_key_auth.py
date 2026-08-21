"""SYNC_API_KEY: opt-in shared-secret auth, off by default.

Starts its own server subprocess with SYNC_API_KEY set (independent of the
main integration test, which deliberately runs with no key -- the demo
default) and checks: no key / wrong key -> 401, matching key -> works, and
a real HttpServer configured with the key completes an end-to-end sync.

Run:  python tests/test_api_key_auth.py
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "client"))

import requests                                  # noqa: E402
from change_detection import StateDB             # noqa: E402
from transport import HttpServer, run_once       # noqa: E402

KEY = "demo-shared-secret"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    workdir = tempfile.mkdtemp(prefix="apikey-test-")
    data_dir = os.path.join(workdir, "srv")
    sync_dir = os.path.join(workdir, "sync")
    os.makedirs(sync_dir)
    port = free_port()
    env = dict(os.environ, SYNC_DATA_DIR=data_dir, SYNC_PORT=str(port), SYNC_API_KEY=KEY)

    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "app.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(30):
            try:
                if requests.get(f"{base}/v1/health", headers={"X-API-Key": KEY},
                                timeout=1).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("server never became healthy")

        r = requests.get(f"{base}/v1/health")
        assert r.status_code == 401, r.status_code
        print("A1. no key ok: 401")

        r = requests.get(f"{base}/v1/health", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401, r.status_code
        print("A2. wrong key ok: 401")

        r = requests.get(f"{base}/v1/health", headers={"X-API-Key": KEY})
        assert r.status_code == 200 and r.json()["ok"] is True
        print("A3. correct key ok: 200")

        # A real client configured with the matching key must be able to
        # sync end-to-end; every request the transport makes (health probes,
        # collect, events, session) has to carry the header, not just GETs.
        srv = HttpServer(base, api_key=KEY)
        srv.wait_healthy()
        db = StateDB(os.path.join(workdir, "client.db"))
        f = os.path.join(sync_dir, "secret.txt")
        with open(f, "w") as fh:
            fh.write("only for holders of the key\n")
        tally = run_once(db, srv, [sync_dir])
        assert tally["synced"] == 1, tally
        r = requests.get(f"{base}/v1/file", params={"path": f}, headers={"X-API-Key": KEY})
        assert r.ok and r.content == b"only for holders of the key\n"
        print("A4. end-to-end sync with a matching client api_key ok")

        print("\nALL API-KEY CHECKS PASSED")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
