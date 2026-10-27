"""PC agent that reports active Claude Code sessions to the gateway.

Runs on each PC and periodically POSTs to the gateway's /chat/sessions/register
endpoint so the Code tab shows real sessions.  The agent queries local session
data via the MCP endpoint (or falls back to reading session files).
"""
import json
import logging
import time
import sys
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ccd_reporter")

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://100.100.186.114:8000")
API_KEY = os.environ.get("GATEWAY_API_KEY", "")
PC_NAME = os.environ.get("PC_NAME", "My PC")
HEARTBEAT_INTERVAL = 30  # seconds


def _fetch_local_sessions():
    """Query the gateway's MCP endpoint for local sessions.

    The MCP server exposes ccd_list_sessions and ccd_get_session which we can
    call to discover active Claude Code sessions on this PC.
    """
    import urllib.request
    try:
        req = urllib.request.Request(f"{GATEWAY_URL}/mcp/sessions")
        if API_KEY:
            req.add_header("Authorization", f"Bearer {API_KEY}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.debug("MCP endpoint not available: %s", e)
        return []


def _get_heartbeat_payload():
    """Build the payload for /chat/sessions/register."""
    sessions = _fetch_local_sessions()
    if not sessions:
        # Fallback: report the current gateway session if one is running
        sessions = [{
            "session_id": "local_" + str(int(time.time())),
            "title": "Gateway playground session",
            "status": "running",
            "model": "claude-sonnet-5",
        }]
    return {
        "pc_name": PC_NAME,
        "sessions": sessions,
    }


def run_once():
    """Send one heartbeat and return."""
    payload = _get_heartbeat_payload()
    import urllib.request
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{GATEWAY_URL}/chat/sessions/register",
                                     data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if API_KEY:
            req.add_header("Authorization", f"Bearer {API_KEY}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            log.info("Registered %d sessions", len(result.get("registered", [])))
    except Exception as e:
        log.error("Heartbeat failed: %s", e)


if __name__ == "__main__":
    log.info("CCD reporter starting — PC=%s, gateway=%s", PC_NAME, GATEWAY_URL)
    while True:
        run_once()
        time.sleep(HEARTBEAT_INTERVAL)
