"""
Chip Watchdog — run from Rundeck every 5 minutes.

Reads C:\\Artighost\\logs\\heartbeat.log (written by agent.py's heartbeat
thread every 2 minutes).

If the last entry is older than STALE_MINUTES or says FAIL:
  - rotate the current agent.py log for forensics
  - kill the python process running agent.py (by command-line match)
  - restart agent.py in a new PowerShell window

Logs every restart decision to C:\\Artighost\\logs\\watchdog-restarts.log

No Slack calls. No emails. No noise.
"""

import os
import sys
import time
import subprocess
from datetime import datetime, timezone

HEARTBEAT_LOG   = r"C:\Artighost\logs\heartbeat.log"
AGENT_LOG       = r"C:\Artighost\logs\agent.log"
RESTART_LOG     = r"C:\Artighost\logs\watchdog-restarts.log"
AGENT_DIR       = r"C:\Artighost"
AGENT_SCRIPT    = "agent.py"
PYTHON_EXE      = "python.exe"
STALE_MINUTES   = 5


def log_restart(msg: str) -> None:
    os.makedirs(os.path.dirname(RESTART_LOG), exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(RESTART_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    # Also print so Rundeck captures it in job output
    print(line, end="")


def rotate_agent_log() -> None:
    """Copy last 500 lines of agent.log to a timestamped file, then truncate."""
    if not os.path.exists(AGENT_LOG):
        return
    try:
        with open(AGENT_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-500:] if len(lines) > 500 else lines
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        snapshot = AGENT_LOG.replace(".log", f".pre-restart-{stamp}.log")
        with open(snapshot, "w", encoding="utf-8") as f:
            f.writelines(tail)
        # truncate active log
        open(AGENT_LOG, "w").close()
    except Exception as e:
        log_restart(f"ROTATE error: {e}")


def kill_agent_processes() -> int:
    """Kill python.exe processes whose command line includes 'agent.py'
    AND the AGENT_DIR path. Returns number killed."""
    try:
        # WMIC is deprecated but still works; use it to get PID + CommandLine
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/format:csv"],
            text=True, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        log_restart(f"PROCESS_LOOKUP error: {e}")
        return 0

    killed = 0
    for line in out.splitlines():
        if "agent.py" not in line:
            continue
        if AGENT_DIR.lower() not in line.lower():
            continue
        # CSV: Node,CommandLine,ProcessId
        parts = line.split(",")
        pid = parts[-1].strip()
        if not pid.isdigit():
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log_restart(f"KILLED pid={pid}")
            killed += 1
        except Exception as e:
            log_restart(f"KILL error pid={pid}: {e}")
    return killed


def launch_agent() -> None:
    """Launch agent.py in a new PowerShell window."""
    cmd = ["powershell.exe", "-NoExit", "-Command", f"cd '{AGENT_DIR}'; {PYTHON_EXE} {AGENT_SCRIPT}"]
    try:
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        log_restart("LAUNCHED new agent.py")
    except Exception as e:
        log_restart(f"LAUNCH error: {e}")


def restart_chip(reason: str) -> None:
    log_restart(f"RESTART reason={reason}")
    rotate_agent_log()
    kill_agent_processes()
    time.sleep(2)
    launch_agent()


def main() -> int:
    # If no heartbeat file at all, agent isn't running — launch it.
    if not os.path.exists(HEARTBEAT_LOG):
        restart_chip("no-heartbeat-file")
        return 0

    try:
        with open(HEARTBEAT_LOG, "r", encoding="utf-8") as f:
            last_line = f.read().strip().splitlines()[-1] if f.read() or True else ""
            # re-read since the first .read() consumed the buffer
    except Exception:
        last_line = ""

    # Simpler re-read
    try:
        with open(HEARTBEAT_LOG, "r", encoding="utf-8") as f:
            content = f.read().strip()
        last_line = content.splitlines()[-1] if content else ""
    except Exception as e:
        restart_chip(f"heartbeat-read-error:{e}")
        return 0

    if not last_line:
        restart_chip("heartbeat-empty")
        return 0

    # Parse timestamp: "2026-10-08T12:34:56.789Z OK" or "...Z FAIL ..."
    try:
        ts_str = last_line.split()[0].rstrip("Z").split(".")[0]
        ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception as e:
        restart_chip(f"heartbeat-parse-error:{e}")
        return 0

    age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
    age_min = age_sec / 60.0

    if age_min > STALE_MINUTES:
        restart_chip(f"stale age={age_min:.1f}min")
        return 0

    if " FAIL" in last_line:
        restart_chip(f"slack-auth-failing last=\"{last_line}\"")
        return 0

    # Healthy. Nothing to do.
    print(f"OK age={age_min:.1f}min last=\"{last_line}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
