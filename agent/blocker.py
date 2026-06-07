"""
Website blocker — client side.
Sends commands to the privileged blocker daemon (com.focus.blocker) via a Unix
socket. The daemon runs as root and owns all /etc/hosts manipulation.
"""
import json
import logging
import socket

SOCKET_PATH = "/tmp/com.focus.blocker.sock"
log = logging.getLogger(__name__)


def _send(cmd: dict) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(SOCKET_PATH)
            s.send((json.dumps(cmd) + "\n").encode())
            resp = b""
            while True:
                chunk = s.recv(4096)
                if not chunk or chunk.endswith(b"\n"):
                    resp += chunk
                    break
                resp += chunk
            return json.loads(resp)
    except FileNotFoundError:
        log.error("Blocker daemon socket not found — is com.focus.blocker loaded?")
        return {"ok": False, "error": "daemon not running"}
    except Exception as e:
        log.error("Blocker daemon error: %s", e)
        return {"ok": False, "error": str(e)}


def is_active() -> bool:
    return _send({"action": "status"}).get("active", False)


def start_block(domains: list[str], until: str) -> None:
    result = _send({"action": "block", "domains": domains, "until": until})
    if result.get("ok"):
        log.info("Block started: %s until %s", domains, until)
    else:
        log.error("Block failed: %s", result.get("error"))


def clear_expired_block() -> None:
    if not is_active():
        _send({"action": "unblock"})
