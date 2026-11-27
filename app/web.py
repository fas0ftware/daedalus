"""Daedalus Web: the coding assistant in the browser (python -m app.web).

Chats in a sidebar, each with its own sandbox folder. Claude answers; every
sandbox run is shown under the answer with its code, output, exit code and
time, and the files a chat wrote can be downloaded. Same agent, same Docker
sandbox and same limits as the Telegram bot; no Telegram token is needed.

    ANTHROPIC_API_KEY=... python -m app.web          # http://127.0.0.1:8745
    python -m app.web --demo                          # scripted replies, real sandbox runs

Local only: see web_base.py.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import quote, unquote

from .agent.core import Agent
from .config import load_web_settings
from .sandbox import RunResult, run_python, workspace_for
from .security import clean_input
from .web_base import App, HttpError, field

HERE = Path(__file__).resolve().parent
NAME_RE = re.compile(r"[\w.\-]{1,100}")
MAX_CHATS = 50
WEB_CHAT_BASE = 9_000_000  # web chat folders: chat-9000001, ... (Telegram ids never collide in practice)


class Chat:
    def __init__(self, number: int, agent_factory, workspace: str):
        self.id = number
        self.title = "New chat"
        self.created = datetime.now().isoformat(timespec="seconds")
        self.workdir = workspace_for(workspace, WEB_CHAT_BASE + number)
        self.items: list[dict] = []  # what the page shows
        self.lock = threading.Lock()
        self.pending: list[dict] = []
        self.agent = agent_factory(self._run)
        self._sandbox_run = None

    def _run(self, code: str) -> RunResult:
        started = time.monotonic()
        result = self._sandbox_run(code, self.workdir)
        self.pending.append({"code": code, "exit_code": result.exit_code, "output": result.output,
                             "timed_out": result.timed_out, "seconds": round(time.monotonic() - started, 2)})
        return result

    def files(self):
        out = []
        for entry in sorted(os.scandir(self.workdir), key=lambda e: e.name):
            if entry.is_file(follow_symlinks=False):
                out.append({"name": entry.name, "bytes": entry.stat().st_size})
        return out

    def summary(self):
        return {"id": self.id, "title": self.title, "created": self.created, "messages": len(self.items),
                "busy": self.lock.locked()}


class Studio:
    def __init__(self, settings, client, sandbox_run=None, demo=False):
        self.settings, self.client, self.demo = settings, client, demo
        self.sandbox_run = sandbox_run or (lambda code, workdir: run_python(
            code, settings.sandbox_image, workdir, settings.sandbox_timeout))
        self.lock = threading.Lock()
        self.chats: dict[int, Chat] = {}
        self.counter = 0

    def new_chat(self) -> Chat:
        with self.lock:
            if len(self.chats) >= MAX_CHATS:
                raise HttpError(409, f"at most {MAX_CHATS} chats; delete one first")
            self.counter += 1
            chat = Chat(self.counter, lambda run: Agent(self.client, self.settings.model, run,
                                                        effort=self.settings.effort,
                                                        max_steps=self.settings.max_steps),
                        self.settings.workspace)
            chat._sandbox_run = self.sandbox_run
            self.chats[chat.id] = chat
            return chat

    def chat(self, chat_id) -> Chat:
        chat = self.chats.get(int(chat_id))
        if not chat:
            raise HttpError(404, "no such chat")
        return chat

    def ask(self, chat: Chat, text: str) -> dict:
        text = clean_input(text)
        if text is None:
            raise HttpError(400, "the message is empty or too long")
        if not chat.lock.acquire(blocking=False):
            raise HttpError(409, "this chat is still working on the previous message")
        try:
            chat.pending = []
            if not chat.items:
                chat.title = " ".join(text.split())[:60]
            chat.items.append({"role": "user", "text": text, "at": datetime.now().isoformat(timespec="seconds")})
            started = time.monotonic()
            try:
                reply = chat.agent.ask(text)
                item = {"role": "assistant", "text": reply.text, "runs": chat.pending, "stop": reply.stop_reason}
            except Exception as exc:  # API or sandbox trouble: shown in the chat, history was rolled back
                item = {"role": "error", "text": f"{type(exc).__name__}: {str(exc)[:300]}", "runs": chat.pending}
            item["seconds"] = round(time.monotonic() - started, 1)
            item["at"] = datetime.now().isoformat(timespec="seconds")
            chat.items.append(item)
            return item
        finally:
            chat.lock.release()

    def state(self):
        return {"model": self.settings.model, "effort": self.settings.effort, "demo": self.demo,
                "docker": shutil.which("docker") is not None, "timeout": self.settings.sandbox_timeout,
                "chats": [c.summary() for c in sorted(self.chats.values(), key=lambda c: -c.id)]}


def build_app(studio: Studio) -> App:
    app = App(HERE / "static", max_body=64 * 1024)

    @app.get("/api/state")
    def state(query):
        return studio.state()

    @app.post("/api/chats")
    def create(body):
        return studio.new_chat().summary()

    @app.get(r"/api/chats/(?P<chat_id>\d{1,6})")
    def show(query, chat_id):
        chat = studio.chat(chat_id)
        return chat.summary() | {"items": chat.items, "files": chat.files()}

    @app.post(r"/api/chats/(?P<chat_id>\d{1,6})/ask")
    def ask(body, chat_id):
        chat = studio.chat(chat_id)
        item = studio.ask(chat, field(body, "text", max_len=8000))
        return {"item": item, "files": chat.files(), "title": chat.title}

    @app.post(r"/api/chats/(?P<chat_id>\d{1,6})/delete")
    def delete(body, chat_id):
        chat = studio.chat(chat_id)
        if chat.lock.locked():
            raise HttpError(409, "this chat is busy")
        with studio.lock:
            studio.chats.pop(chat.id, None)
        return studio.state()

    @app.get(r"/api/chats/(?P<chat_id>\d{1,6})/files/(?P<name>[^/]{1,100})")
    def download(query, chat_id, name):
        chat = studio.chat(chat_id)
        name = unquote(name)  # the page sends encodeURIComponent(name): "çıktı.txt" arrives as %C3%A7...
        if not NAME_RE.fullmatch(name) or name.startswith("."):
            raise HttpError(404, "not found")
        path = Path(chat.workdir) / name
        if not path.is_file() or path.is_symlink() or path.resolve().parent != Path(chat.workdir).resolve():
            raise HttpError(404, "not found")
        # HTTP headers are Latin-1 only: an ASCII fallback name plus the real one in RFC 5987 form
        plain = re.sub(r"[^A-Za-z0-9._-]+", "_", name.encode("ascii", "replace").decode()) or "file"
        return path.read_bytes(), "application/octet-stream", {
            "Content-Disposition": f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name)}"}

    return app


class ScriptedClient:
    """--demo: answers two prepared questions; the code really runs in the sandbox."""

    SCRIPT = [
        ("prime", "Let me check that in the sandbox with two independent methods.", """\
import time

def sieve(limit):
    flags = bytearray([1]) * (limit + 1)
    flags[0:2] = b"\\x00\\x00"
    for i in range(2, int(limit ** 0.5) + 1):
        if flags[i]:
            flags[i * i::i] = bytearray(len(flags[i * i::i]))
    return [i for i, f in enumerate(flags) if f]

def is_prime(n):
    return n > 1 and all(n % d for d in range(2, int(n ** 0.5) + 1))

t = time.perf_counter()
primes = sieve(10_000)
p = primes[999]
print(f"1000th prime (sieve): {p}")
print(f"trial division agrees: {is_prime(p)} and {sum(map(is_prime, range(p + 1)))} primes up to it")
print(f"took {(time.perf_counter() - t) * 1000:.1f} ms")
""", "Both methods agree.\n\n**{0}**, and counting primes by trial division up to it gives exactly 1000, so the index is right.\n\n```python\nprimes = sieve(10_000)\nprimes[999]\n```\n\nThe sieve marks multiples of each prime once, which is why it stays fast even for millions of numbers."),
        ("fib", "I'll write the file in this chat's folder and read it back.", """\
import csv, os

a, b = 0, 1
with open("fib.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["n", "fib"])
    for n in range(20):
        w.writerow([n, a])
        a, b = b, a + b

rows = list(csv.reader(open("fib.csv")))
print(f"rows: {len(rows) - 1}, last: {rows[-1][1]}, size: {os.path.getsize('fib.csv')} bytes")
""", "Done: `fib.csv` is in this chat's folder (download it on the right).\n\n**{0}**\n\nThe folder persists for this chat, so the next run can read the file again."),
    ]

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        history = kw["messages"]
        last = history[-1]
        if isinstance(last["content"], list) and last["content"] and isinstance(last["content"][0], dict) \
                and last["content"][0].get("type") == "tool_result":
            key = next(k for k, *_ in self.SCRIPT if any(
                isinstance(m["content"], str) and k in m["content"].lower() for m in history[-4:]))
            reply = next(r for k, _, _, r in self.SCRIPT if k == key)
            first = last["content"][0]["content"].split("\n", 2)[1]
            return NS(stop_reason="end_turn", content=[NS(type="text", text=reply.format(first))])
        text = last["content"].lower() if isinstance(last["content"], str) else ""
        for key, intro, code, _ in self.SCRIPT:
            if key in text:
                return NS(stop_reason="tool_use", content=[NS(type="text", text=intro),
                                                           NS(type="tool_use", id=f"demo-{key}", name="run_python",
                                                              input={"code": code})])
        return NS(stop_reason="end_turn", content=[NS(type="text", text=(
            "Demo mode answers two prepared questions (try the 1000th prime, or the Fibonacci file). "
            "Set ANTHROPIC_API_KEY and start without --demo for real answers."))])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Daedalus Web: the coding assistant in the browser (local only)")
    parser.add_argument("--port", type=int, default=8745)
    parser.add_argument("--demo", action="store_true", help="scripted replies (no API key), real sandbox runs")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    try:
        settings = load_web_settings(os.environ, demo=args.demo)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not shutil.which("docker"):
        print("Docker is required for the sandbox (see README).", file=sys.stderr)
        return 2
    if args.demo:
        client = ScriptedClient()
    else:
        import anthropic
        client = anthropic.Anthropic()
    build_app(Studio(settings, client, demo=args.demo)).serve(
        args.port, f"Daedalus Web ({'demo' if args.demo else settings.model})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
