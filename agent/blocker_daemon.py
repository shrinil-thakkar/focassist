"""
Privileged blocking daemon — installed in /Library/LaunchDaemons and runs as root.
Listens on a Unix socket for block/unblock commands from the Mac agent.

Two-layer blocking to defeat Chrome's DNS-over-HTTPS:
  1. /etc/hosts  — blocks system DNS (most apps)
  2. pf anchor   — blocks at packet level (catches Chrome DoH, any app)

Protocol: newline-terminated JSON → JSON response.
Commands:
  {"action": "block",   "domains": [...], "until": "<ISO UTC>"}
  {"action": "unblock"}
  {"action": "status"}  → {"ok": true, "active": bool}
"""
import json
import logging
import os
import socket as _socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SOCKET_PATH  = "/tmp/com.focus.blocker.sock"
HOSTS_FILE   = Path("/etc/hosts")
LOCK_FILE    = Path("/var/run/focassist-block.lock")
PF_ANCHOR    = "com.focus.blocker"
MARKER_START = "# focassist-block-start"
MARKER_END   = "# focassist-block-end"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s focassist.blocker-daemon: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_active() -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        lock = json.loads(LOCK_FILE.read_text())
        until = datetime.fromisoformat(lock["until"])
        return datetime.now(timezone.utc) < until
    except Exception:
        return False


def _resolve_ips(domains: list[str]) -> list[str]:
    """Resolve domains to IPv4 before modifying /etc/hosts (which would affect resolution)."""
    ips = set()
    for domain in domains:
        for d in (domain, f"www.{domain}"):
            try:
                for res in _socket.getaddrinfo(d, 80, _socket.AF_INET):
                    ips.add(res[4][0])
            except Exception:
                pass
    return list(ips)


def _ensure_pf_anchor() -> None:
    """Add our anchor line to /etc/pf.conf if not already present."""
    pf_conf = Path("/etc/pf.conf")
    if not pf_conf.exists():
        return
    content = pf_conf.read_text()
    anchor_line = f'anchor "{PF_ANCHOR}"'
    if anchor_line not in content:
        with open(pf_conf, "a") as f:
            f.write(f"\n{anchor_line}\n")
        # Reload pf.conf so the anchor is registered
        subprocess.run(["pfctl", "-f", str(pf_conf)], check=False, capture_output=True)
        log.info("Added pf anchor to /etc/pf.conf")


def _apply_pf(ips: list[str]) -> None:
    if not ips:
        return
    _ensure_pf_anchor()
    ip_list = " ".join(ips)
    rules = (
        f'table <focassist_block> {{ {ip_list} }}\n'
        f'block drop out quick proto {{tcp udp}} from any to <focassist_block>\n'
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".rules", delete=False) as f:
        f.write(rules)
        rules_file = f.name
    subprocess.run(["pfctl", "-a", PF_ANCHOR, "-f", rules_file], check=False, capture_output=True)
    subprocess.run(["pfctl", "-e"], check=False, capture_output=True)
    os.unlink(rules_file)
    log.info("pf rules applied for %d IPs", len(ips))


def _clear_pf() -> None:
    subprocess.run(["pfctl", "-a", PF_ANCHOR, "-F", "all"], check=False, capture_output=True)
    log.info("pf rules cleared")


# ── Block / unblock ───────────────────────────────────────────────────────────

def _apply_block(domains: list[str], until: str) -> None:
    if _is_active():
        log.info("Block already active — skipping.")
        return

    # Resolve IPs NOW, before /etc/hosts intercepts DNS
    ips = _resolve_ips(domains)
    log.info("Resolved %d IPs for %s", len(ips), domains)

    # Write lock
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({"until": until, "domains": domains, "ips": ips}))

    # Layer 1: /etc/hosts
    lines = [f"\n{MARKER_START}\n"]
    for d in domains:
        lines.append(f"0.0.0.0 {d}\n")
        lines.append(f"0.0.0.0 www.{d}\n")
    lines.append(f"{MARKER_END}\n")
    with open(HOSTS_FILE, "a") as f:
        f.writelines(lines)

    # Flush system DNS cache
    subprocess.run(["dscacheutil", "-flushcache"], check=False)
    subprocess.run(["killall", "-HUP", "mDNSResponder"], check=False)

    # Layer 2: pf packet filter (blocks Chrome DoH and all other traffic)
    _apply_pf(ips)

    log.info("Block applied: %s until %s", domains, until)


def _remove_block() -> None:
    # Layer 1: /etc/hosts
    content = HOSTS_FILE.read_text()
    if MARKER_START in content:
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

    # Layer 2: pf
    _clear_pf()

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
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)
    server.listen(5)
    log.info("Listening on %s", SOCKET_PATH)

    # Clear any expired block from before restart
    if not _is_active() and LOCK_FILE.exists():
        _remove_block()

    while True:
        conn, _ = server.accept()
        try:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk or chunk.endswith(b"\n"):
                    data += chunk
                    break
                data += chunk
            if data:
                cmd = json.loads(data)
                result = _handle(cmd)
                conn.send((json.dumps(result) + "\n").encode())
        except Exception as e:
            log.error("Error: %s", e)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
