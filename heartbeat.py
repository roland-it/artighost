"""
Heartbeat module for Chip.

Spawns a background daemon thread that calls Slack's auth.test every N
seconds. If auth.test succeeds, the connection + token are healthy. The
result is written to HEARTBEAT_LOG as a one-line timestamp + status.

Watchdog (watchdog.ps1 / watchdog.py) reads that file. If the last entry
is older than ~5 minutes or says "FAIL", the watchdog restarts agent.py.

Import this module from agent.py and call start_heartbeat() once, after
the Slack app and token have been loaded but before handler.start().

No Slack events are sent — nothing shows up in any user's DMs. auth.test
is a free, no-side-effect API call.
"""

import os
import threading
import time
import logging
from datetime import datetime
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

HEARTBEAT_LOG = r"C:\Artighost\logs\heartbeat.log"
HEARTBEAT_INTERVAL_SECONDS = 120  # 2 minutes — well under the 5-min watchdog threshold


def _write(line: str) -> None:
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_LOG), exist_ok=True)
        with open(HEARTBEAT_LOG, "w", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        log.warning(f"heartbeat write failed: {e}")


def _heartbeat_loop() -> None:
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    while True:
        try:
            client.auth_test()
            _write(f"{datetime.utcnow().isoformat()}Z OK")
        except SlackApiError as e:
            _write(f"{datetime.utcnow().isoformat()}Z FAIL slack:{e.response.get('error','unknown')}")
        except Exception as e:
            _write(f"{datetime.utcnow().isoformat()}Z FAIL other:{type(e).__name__}")
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


def start_heartbeat() -> None:
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="chip-heartbeat")
    t.start()
    log.info(f"Heartbeat started → {HEARTBEAT_LOG} every {HEARTBEAT_INTERVAL_SECONDS}s")
