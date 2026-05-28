"""
nira-process-forge — Full process lifecycle manager MCP server.
Spawns, tracks, kills, and tails long-running processes across tool calls.
Registry lives in memory (lost on MCP restart — by design).
"""

from mcp.server.fastmcp import FastMCP
import os
import json
import uuid
import time
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOG_DIR = Path(os.environ.get("PROC_FORGE_LOG_DIR", str(Path.home() / "proc_forge_logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)  # noqa

PYTHONW = os.environ.get("PYTHONW_PATH", "pythonw")

# Windows creation flags
DETACHED_PROCESS     = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# ---------------------------------------------------------------------------
# In-memory registry: { id -> {pid, name, cmd, started, status, log_file, proc} }
# ---------------------------------------------------------------------------

_registry: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proc_status_str(entry: dict) -> str:
    proc = entry.get("proc")
    if proc is None:
        return "detached"
    rc = proc.poll()
    return "stopped" if rc is not None else "running"


def _entry_summary(proc_id: str, entry: dict) -> dict:
    status = _proc_status_str(entry)
    proc = entry.get("proc")
    rc = proc.poll() if proc else None
    started_ts = entry.get("started", 0)
    uptime = round(time.time() - started_ts, 1) if started_ts else None
    return {
        "id": proc_id,
        "pid": entry.get("pid"),
        "name": entry.get("name"),
        "cmd": entry.get("cmd"),
        "started": entry.get("started_str"),
        "status": status,
        "returncode": rc,
        "uptime_s": uptime,
        "log_file": entry.get("log_file"),
    }


def _pid_alive(pid: int) -> bool:
    """Check if a PID is still alive via tasklist (handles detached procs)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5
        )
        return str(pid) in result.stdout
    except Exception:
        return False

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "nira-process-forge",
    instructions=(
        "Full process lifecycle manager. Spawn, monitor, kill, and tail "
        "long-running processes. Registry is in-memory (lost on MCP restart)."
    ),
)


@mcp.tool()
def proc_spawn(
    name: str,
    cmd: str,
    cwd: str = None,
    env_extra: dict = None,
    detach: bool = True,
) -> dict:
    """
    Spawn a process and register it.

    Args:
        name: Human-readable label for this process.
        cmd: Full command string to execute (passed to shell=True).
        cwd: Working directory. Defaults to current directory.
        env_extra: Extra env vars to merge into the environment.
        detach: If True, use DETACHED_PROCESS flag (process survives MCP exit).

    Returns:
        {id, pid, name, cmd, log_file, started}
    """
    proc_id = str(uuid.uuid4())
    log_file = str(LOG_DIR / f"{proc_id}.log")

    env = os.environ.copy()
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})

    flags = 0
    if detach:
        flags |= DETACHED_PROCESS

    try:
        log_fh = open(log_file, "w", encoding="utf-8", buffering=1)
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd or None,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    except Exception as exc:
        return {"error": str(exc), "id": proc_id}

    started_ts = time.time()
    started_str = datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d %H:%M:%S")

    _registry[proc_id] = {
        "pid": proc.pid,
        "name": name,
        "cmd": cmd,
        "started": started_ts,
        "started_str": started_str,
        "log_file": log_file,
        "proc": proc,
        "log_fh": log_fh,
        "detach": detach,
    }

    return {
        "id": proc_id,
        "pid": proc.pid,
        "name": name,
        "cmd": cmd,
        "log_file": log_file,
        "started": started_str,
    }


@mcp.tool()
def proc_status(proc_id: str = None) -> dict:
    """
    Get status of one or all registered processes.

    Args:
        proc_id: If given, return status of this process. If None, return all.

    Returns:
        Single process dict or {processes: [list], count: int}.
    """
    if proc_id is not None:
        entry = _registry.get(proc_id)
        if entry is None:
            return {"error": f"Process {proc_id!r} not found in registry"}
        return _entry_summary(proc_id, entry)

    summaries = [_entry_summary(pid, e) for pid, e in _registry.items()]
    return {"processes": summaries, "count": len(summaries)}


@mcp.tool()
def proc_kill(proc_id: str, force: bool = True) -> dict:
    """
    Kill a registered process.

    Args:
        proc_id: Registry ID of the process.
        force: If True, use kill() (SIGKILL). If False, use terminate() (SIGTERM).

    Returns:
        {killed: bool, returncode, pid, name}
    """
    entry = _registry.get(proc_id)
    if entry is None:
        return {"killed": False, "error": f"Process {proc_id!r} not in registry"}

    proc = entry.get("proc")
    pid = entry.get("pid")
    name = entry.get("name")

    # Already dead?
    if proc is not None and proc.poll() is not None:
        return {"killed": True, "returncode": proc.returncode, "pid": pid, "name": name, "note": "already_stopped"}

    killed = False
    returncode = None

    # Try via subprocess.Popen handle first
    if proc is not None:
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            returncode = proc.returncode
            killed = True
        except Exception as exc:
            # Fall back to taskkill (handles elevated processes)
            pass

    # Fallback: taskkill by PID
    if not killed and pid:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True, text=True, timeout=10
            )
            killed = result.returncode == 0
            returncode = result.returncode
        except Exception as exc:
            return {"killed": False, "error": str(exc), "pid": pid, "name": name}

    # Update registry status
    if killed:
        entry["status"] = "stopped"

    return {"killed": killed, "returncode": returncode, "pid": pid, "name": name}


@mcp.tool()
def proc_log_tail(proc_id: str, lines: int = 50) -> dict:
    """
    Read the last N lines from a process's log file.

    Args:
        proc_id: Registry ID of the process.
        lines: Number of tail lines to return (default 50, max 500).

    Returns:
        {log: str, lines: int, log_file: str}
    """
    lines = min(lines, 500)

    entry = _registry.get(proc_id)
    if entry is None:
        return {"error": f"Process {proc_id!r} not in registry"}

    log_file = entry.get("log_file")
    if not log_file or not Path(log_file).exists():
        return {"error": "Log file not found", "log_file": log_file}

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
        tail = all_lines[-lines:]
        log_text = "".join(tail)
        return {"log": log_text, "lines": len(tail), "log_file": log_file}
    except Exception as exc:
        return {"error": str(exc), "log_file": log_file}


@mcp.tool()
def proc_list() -> dict:
    """
    List all tracked processes with live status.
    Also checks detached processes by PID to see if they are still alive.

    Returns:
        {processes: [...], count: int, running: int, stopped: int}
    """
    result = []
    running = 0
    stopped = 0

    for proc_id, entry in _registry.items():
        summary = _entry_summary(proc_id, entry)

        # For detached processes where we have no Popen handle, check via PID
        if summary["status"] == "detached" and entry.get("pid"):
            alive = _pid_alive(entry["pid"])
            summary["status"] = "running" if alive else "stopped"

        if summary["status"] == "running":
            running += 1
        else:
            stopped += 1

        result.append(summary)

    return {
        "processes": result,
        "count": len(result),
        "running": running,
        "stopped": stopped,
    }


@mcp.tool()
def proc_spawn_pythonw(
    name: str,
    script_path: str,
    args: list = None,
    cwd: str = None,
) -> dict:
    """
    Convenience wrapper for spawning pythonw.exe daemon scripts.
    Uses the aria_brain conda env's pythonw.exe.
    Adds CREATE_NEW_PROCESS_GROUP so the process survives Windows job object kills.

    Args:
        name: Human-readable label.
        script_path: Absolute path to the .py script.
        args: Optional list of additional CLI args.
        cwd: Working directory (defaults to script's parent).

    Returns:
        {id, pid, name, cmd, log_file, started}
    """
    proc_id = str(uuid.uuid4())
    log_file = str(LOG_DIR / f"{proc_id}.log")

    script_path = str(script_path)
    args = args or []
    arg_str = " ".join(str(a) for a in args)
    cmd_parts = [f'"{PYTHONW}"', f'"{script_path}"']
    if arg_str:
        cmd_parts.append(arg_str)
    cmd = " ".join(cmd_parts)

    resolved_cwd = cwd or str(Path(script_path).parent)

    env = os.environ.copy()

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    try:
        log_fh = open(log_file, "w", encoding="utf-8", buffering=1)
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=resolved_cwd,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    except Exception as exc:
        return {"error": str(exc), "id": proc_id}

    started_ts = time.time()
    started_str = datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d %H:%M:%S")

    _registry[proc_id] = {
        "pid": proc.pid,
        "name": name,
        "cmd": cmd,
        "started": started_ts,
        "started_str": started_str,
        "log_file": log_file,
        "proc": proc,
        "log_fh": log_fh,
        "detach": True,
        "pythonw": True,
    }

    return {
        "id": proc_id,
        "pid": proc.pid,
        "name": name,
        "cmd": cmd,
        "log_file": log_file,
        "started": started_str,
    }


if __name__ == "__main__":
    mcp.run()
