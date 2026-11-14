"""Runs Python code in a locked-down Docker container.

No network, read-only root, no capabilities, non-root user, memory/CPU/process limits,
a hard timeout, and only the chat's own workspace folder mounted."""

from __future__ import annotations

import os
import re
import secrets
import subprocess  # docker is called with an argument list, never a shell  # nosec B404
import threading
import time
from dataclasses import dataclass

MAX_CODE = 20_000
MAX_OUTPUT = 4000
# Bytes read per stream (stdout, stderr). The container's memory limit does not cover what it
# prints into the pipe, so without this a run that prints for 20 s fills the bot's own memory.
MAX_CAPTURE = 64 * 1024


@dataclass
class RunResult:
    exit_code: int
    output: str
    timed_out: bool = False


SANDBOX_UID = 10001  # the sandbox image's own non-root user


def container_user() -> str:
    """Same uid/gid as the host user, so files stay owned by you; never root."""
    if hasattr(os, "getuid") and os.getuid() != 0:
        return f"{os.getuid()}:{os.getgid()}"
    return f"{SANDBOX_UID}:{SANDBOX_UID}"


def docker_command(image: str, workdir: str, timeout: int, name: str = "") -> list[str]:
    return [
        "docker", "run", "--rm", "-i", *(["--name", name] if name else []),
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m",  # tmpfs inside the container  # nosec B108
        "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5", "--pids-limit", "64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", container_user(),
        "-v", f"{os.path.abspath(workdir)}:/workspace:rw", "-w", "/workspace",
        image, "timeout", "-s", "KILL", str(timeout), "python", "-I", "-",
    ]


def workspace_for(base: str, chat_id: int) -> str:
    path = os.path.join(base, f"chat-{int(chat_id)}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    if hasattr(os, "getuid") and os.getuid() == 0:
        # Running as root: the code runs as the sandbox user, which must be able to write here.
        os.chown(path, SANDBOX_UID, SANDBOX_UID)
    return path


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return text if len(text) <= limit else text[: limit // 2] + "\n…[output truncated]…\n" + text[-limit // 2:]


def kill_container(name: str) -> None:
    """Stop a sandbox container by name. Killing the `docker run` client alone leaves it running."""
    try:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=10, check=False)  # nosec B603 B607
    except (OSError, subprocess.TimeoutExpired):
        pass


@dataclass
class Captured:
    returncode: int
    stdout: str
    stderr: str
    overflow: bool = False   # printed more than MAX_CAPTURE: the run was stopped


def capped_run(cmd: list[str], input: str, timeout: float, container: str = "",
               capture: int = MAX_CAPTURE, kill=kill_container) -> Captured:
    """Like subprocess.run(capture_output=True, text=True), but keeps at most `capture` bytes per
    stream, and stops the container (by name) as soon as it prints more or the time is up.

    Raises subprocess.TimeoutExpired after the container has been stopped.
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,  # nosec B603
                            stderr=subprocess.PIPE)
    overflow = threading.Event()
    kept = {"out": bytearray(), "err": bytearray()}

    def feed():
        try:
            proc.stdin.write(input.encode())
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # the code was cut off: the program exited or was stopped

    def drain(stream, sink: bytearray):
        while chunk := stream.read1(65536):
            room = capture - len(sink)
            sink += chunk[:max(room, 0)]
            if len(chunk) > room:
                overflow.set()  # keep reading (and discarding) until the stopped process closes the pipe

    threads = [threading.Thread(target=feed, daemon=True),
               threading.Thread(target=drain, args=(proc.stdout, kept["out"]), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, kept["err"]), daemon=True)]
    for t in threads:
        t.start()

    def stop():
        if container:
            kill(container)
        proc.kill()

    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if overflow.is_set():
            stop()
            break
        if time.monotonic() > deadline:
            stop()
            proc.wait()
            raise subprocess.TimeoutExpired(cmd, timeout)
        time.sleep(0.02)
    proc.wait()
    for t in threads[1:]:
        t.join(timeout=5)
    decode = lambda b: bytes(b).decode("utf-8", "replace")  # noqa: E731
    return Captured(proc.returncode, decode(kept["out"]), decode(kept["err"]), overflow.is_set())


def run_python(code: str, image: str, workdir: str, timeout: int, runner=None) -> RunResult:
    if not code.strip():
        return RunResult(2, "error: empty code")
    if len(code) > MAX_CODE:
        return RunResult(2, f"error: code longer than {MAX_CODE} characters")
    try:
        if runner is None:
            name = f"daedalus-run-{secrets.token_hex(6)}"
            proc = capped_run(docker_command(image, workdir, timeout, name), code, timeout + 15, container=name)
        else:
            proc = runner(docker_command(image, workdir, timeout), input=code, capture_output=True, text=True,
                          timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        return RunResult(124, "error: timed out", timed_out=True)
    except FileNotFoundError:
        return RunResult(127, "error: docker is not installed")
    output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    if getattr(proc, "overflow", False):
        output += f"\n[stopped: the code printed more than {MAX_CAPTURE // 1024} kB]"
        return RunResult(proc.returncode, truncate(output.strip()), timed_out=False)
    # 124/137: killed by `timeout -s KILL` or by the kernel's out-of-memory killer.
    timed_out = proc.returncode in (124, 137)
    if timed_out:
        output += f"\n[killed: time limit ({timeout}s) or memory limit reached]"
    return RunResult(proc.returncode, truncate(output.strip()) or "(no output)", timed_out)
