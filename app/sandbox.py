"""Runs Python code in a locked-down Docker container.

No network, read-only root, no capabilities, non-root user, memory/CPU/process limits,
a hard timeout, and only the chat's own workspace folder mounted."""

from __future__ import annotations

import os
import re
import subprocess  # docker is called with an argument list, never a shell  # nosec B404
from dataclasses import dataclass

MAX_CODE = 20_000
MAX_OUTPUT = 4000


@dataclass
class RunResult:
    exit_code: int
    output: str
    timed_out: bool = False


def docker_command(image: str, workdir: str, timeout: int) -> list[str]:
    return [
        "docker", "run", "--rm", "-i",
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m",  # tmpfs inside the container  # nosec B108
        "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5", "--pids-limit", "64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        # Same uid/gid as the host user (never root), so the chat folder stays writable
        # for the code and owned by you on the host.
        "--user", f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and os.getuid() != 0 else "10001:10001",
        "-v", f"{os.path.abspath(workdir)}:/workspace:rw", "-w", "/workspace",
        image, "timeout", "-s", "KILL", str(timeout), "python", "-I", "-",
    ]


def workspace_for(base: str, chat_id: int) -> str:
    path = os.path.join(base, f"chat-{int(chat_id)}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return text if len(text) <= limit else text[: limit // 2] + "\n…[output truncated]…\n" + text[-limit // 2:]


def run_python(code: str, image: str, workdir: str, timeout: int, runner=subprocess.run) -> RunResult:
    if not code.strip():
        return RunResult(2, "error: empty code")
    if len(code) > MAX_CODE:
        return RunResult(2, f"error: code longer than {MAX_CODE} characters")
    try:
        proc = runner(docker_command(image, workdir, timeout), input=code, capture_output=True, text=True,
                      timeout=timeout + 15)  # nosec B603
    except subprocess.TimeoutExpired:
        return RunResult(124, "error: timed out", timed_out=True)
    except FileNotFoundError:
        return RunResult(127, "error: docker is not installed")
    output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    timed_out = proc.returncode in (124, 137)
    if timed_out:
        output += f"\n[killed after {timeout}s]"
    return RunResult(proc.returncode, truncate(output.strip()) or "(no output)", timed_out)
