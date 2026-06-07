"""
Website blocker for macOS.
v1: hosts-file block (irreversible until the block expires or sudo clears it).
SelfControl CLI is preferred if installed; falls back to /etc/hosts manipulation.

IMPORTANT: The block is intentionally hard to undo — that's the point.
The agent writes a lock file so it won't double-block an active session.
"""
import os
import subprocess
import logging
import json
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_FILE = Path.home() / ".focassist" / "block.lock"
HOSTS_MARKER_START = "# focassist-block-start"
HOSTS_MARKER_END = "# focassist-block-end"
HOSTS_FILE = Path("/etc/hosts")


def _selfcontrol_cli() -> str | None:
    """Return path to SelfControl CLI if installed."""
    candidates = [
        "/Applications/SelfControl.app/Contents/MacOS/selfcontrol-cli",
        "/usr/local/bin/selfcontrol-cli",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def is_active() -> bool:
    """True if a block is currently active according to the lock file."""
    if not LOCK_FILE.exists():
        return False
    try:
        lock = json.loads(LOCK_FILE.read_text())
        until = datetime.fromisoformat(lock["block_until"])
        return datetime.now(timezone.utc) < until
    except Exception:
        return False


def start_block(domains: list[str], until: str) -> None:
    """
    Block the given domains until the ISO-8601 UTC timestamp `until`.
    Writes a lock file; actual blocking via SelfControl or hosts file.
    """
    if is_active():
        log.info("Block already active — skipping.")
        return

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({"block_until": until, "domains": domains}))

    sc = _selfcontrol_cli()
    if sc:
        _start_selfcontrol(sc, domains, until)
    else:
        _start_hosts_block(domains)

    log.info("Block started: %s until %s", domains, until)


def _start_selfcontrol(cli: str, domains: list[str], until: str) -> None:
    until_dt = datetime.fromisoformat(until)
    now = datetime.now(timezone.utc)
    duration_min = max(1, int((until_dt - now).total_seconds() / 60))

    # SelfControl blocklist is a plist; we build a temporary one
    import plistlib, tempfile
    blocklist = [{"host": d} for d in domains]
    plist_data = {"HostBlacklist": blocklist}
    with tempfile.NamedTemporaryFile(suffix=".plist", delete=False) as f:
        plistlib.dump(plist_data, f, fmt=plistlib.FMT_XML)
        plist_path = f.name

    subprocess.run(
        ["sudo", cli, "--install", "--blocklist", plist_path,
         "--duration", str(duration_min)],
        check=True,
    )


def _start_hosts_block(domains: list[str]) -> None:
    """Append blocking entries to /etc/hosts. Requires passwordless sudo for this file."""
    lines = [f"\n{HOSTS_MARKER_START}\n"]
    for d in domains:
        lines.append(f"0.0.0.0 {d}\n")
        lines.append(f"0.0.0.0 www.{d}\n")
    lines.append(f"{HOSTS_MARKER_END}\n")
    block_text = "".join(lines)

    # Write to a temp file then append via sudo tee -a
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(block_text)
        tmp = f.name

    subprocess.run(
        ["sudo", "tee", "-a", str(HOSTS_FILE)],
        input=block_text, text=True, check=True, capture_output=True
    )
    # Flush DNS cache
    subprocess.run(["sudo", "dscacheutil", "-flushcache"], check=False)
    subprocess.run(["sudo", "killall", "-HUP", "mDNSResponder"], check=False)


def clear_expired_block() -> None:
    """Remove the hosts-file block if the lock has expired (called by the agent on poll)."""
    if is_active():
        return
    if not LOCK_FILE.exists():
        return

    try:
        lock = json.loads(LOCK_FILE.read_text())
    except Exception:
        LOCK_FILE.unlink(missing_ok=True)
        return

    _remove_hosts_block()
    LOCK_FILE.unlink(missing_ok=True)
    log.info("Expired block cleared.")


def _remove_hosts_block() -> None:
    content = HOSTS_FILE.read_text()
    if HOSTS_MARKER_START not in content:
        return
    lines = content.splitlines(keepends=True)
    in_block = False
    cleaned = []
    for line in lines:
        if HOSTS_MARKER_START in line:
            in_block = True
            continue
        if HOSTS_MARKER_END in line:
            in_block = False
            continue
        if not in_block:
            cleaned.append(line)

    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.writelines(cleaned)
        tmp = f.name

    subprocess.run(["sudo", "cp", tmp, str(HOSTS_FILE)], check=True)
    subprocess.run(["sudo", "dscacheutil", "-flushcache"], check=False)
    subprocess.run(["sudo", "killall", "-HUP", "mDNSResponder"], check=False)
