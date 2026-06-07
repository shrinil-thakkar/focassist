"""
Privileged blocking daemon — installed in /Library/LaunchDaemons and runs as root.
Listens on a Unix socket for block/unblock commands from the Mac agent (which runs
as a normal user and cannot write to /etc/hosts itself).

Protocol: newline-terminated JSON  →  JSON response.
Commands:
  {"action": "block",   "domains": [...], "until": "<ISO UTC>"}
  {"action": "unblock"}
  {"action": "status"}   → {"ok": true, "active": bool}
"""
import json
import logging
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SOCKET_PATH = "/tmp/com.focus.blocker.sock"
HOSTS_FILE   = Path("/etc/hosts")
LOCK_FILE    = Path("/var/run/focassist-block.lock")
MARKER_START = "# focassist-block-start"
MARKER_END   = "# focassist-block-end"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s focassist.blocker-daemon: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Block state ───────────────────────────────────────────────────────────────

def _is_active() -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        lock = json.loads(LOCK_FILE.read_text())
        until = datetime.fromisoformat(lock["until"])
        return datetime.now(timezone.utc) < until
    except Exception:
        return False


def _apply_block(domains: list[str], until: str) -> None:
    if _is_active():
        log.info("Block already active — skipping.")
        return

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({"until": until, "domains": domains}))

    lines = [f"\n{MARKER_START}\n"]
    for d in domains:
        lines.append(f"0.0.0.0 {d}\n")
        lines.append(f"0.0.0.0 www.{d}\n")
    lines.append(f"{MARKER_END}\n")

    with open(HOSTS_FILE, "a") as f:
        f.writelines(lines)

    subprocess.run(["dscacheutil", "-flushcache"], check=False)
    subprocess.run(["killall", "-HUP", "mDNSResponder"], check=False)
    log.info("Block applied: %s until %s", domains, until)


def _remove_block() -> None:
    content = HOSTS_FILE.read_text()
    if MARKER_START not in content:
        LOCK_FILE.unlink(missing_ok=True)
        return

    in_block = False
    cleaned = []
    for line in content.splitlines(keepends=True):
        if MARKER_START in line:
            in_block = True
            continue
        if MARKER_END in line:
            in_block = False
            continue
        if not in_block:
            cleaned.append(line)

    HOSTS_FILE.write_text("".join(cleaned))
    subprocess.run(["dscacheutil", "-flushcache"], check=False)
    subprocess.run(["killall", "-HUP", "mDNSResponder"], check=False)
    LOCK_FILE.unlink(missing_ok=True)
    log.info("Block removed.")


# ── Command handler ───────────────────────────────────────────────────────────

def _handle(cmd: dict) -> dict:
    action = cmd.get("action")
    if action == "block":
        _apply_block(cmd.get("domains", []), cmd.get("until", ""))
        return {"ok": True}
    if action == "unblock":
        _remove_block()
        return {"ok": True}
    if action == "status":
        return {"ok": True, "active": _is_active()}
    return {"ok": False, "error": f"unknown action: {action}"}


# ── Server loop ───────────────────────────────────────────────────────────────

def main() -> None:
    # Clean up stale socket from a previous run
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)  # allow any local user to connect
    server.listen(5)
    log.info("Listening on %s", SOCKET_PATH)

    # Clear any expired block that survived a reboot
    if not _is_active() and LOCK_FILE.exists():
        _remove_block()

    while True:
        conn, _ = server.accept()
        try:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b"\n"):
                    break
            if data:
                cmd = json.loads(data)
                result = _handle(cmd)
                conn.send((json.dumps(result) + "\n").encode())
        except Exception as e:
            log.error("Error handling connection: %s", e)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
