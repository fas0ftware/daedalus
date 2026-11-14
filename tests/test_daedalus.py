import subprocess
from types import SimpleNamespace as NS

import pytest

from app.agent.core import MAX_HISTORY_MESSAGES, RUN_PYTHON_TOOL, Agent
from app.config import load_settings
from app.handlers.telegram import chunks
from app.sandbox import RunResult, docker_command, run_python, truncate, workspace_for
from app.security import RateLimiter, clean_input, is_allowed


def text(t):
    return NS(type="text", text=t)


def tool(id_, code):
    return NS(type="tool_use", id=id_, name="run_python", input={"code": code})


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.messages = self

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return self.responses.pop(0)


def agent(responses, runner=None, **kw):
    client = FakeClient(responses)
    runs = []

    def run(code):
        runs.append(code)
        return runner(code) if runner else RunResult(0, "4")
    return Agent(client, "claude-haiku-5-5", run, **kw), client, runs


# --- agent -----------------------------------------------------------------------

def test_plain_answer():
    a, client, runs = agent([NS(stop_reason="end_turn", content=[text("Hello!")])])
    reply = a.ask("hi")
    assert reply.text == "Hello!" and runs == []
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["tools"] == [RUN_PYTHON_TOOL]
    assert call["output_config"] == {"effort": "medium"}


def test_tool_loop_runs_code_and_returns_results_in_one_message():
    responses = [
        NS(stop_reason="tool_use", content=[text("Checking."), tool("t1", "print(2+2)"), tool("t2", "print(1)")]),
        NS(stop_reason="end_turn", content=[text("It is 4.")]),
    ]
    a, client, runs = agent(responses)
    reply = a.ask("2+2?")
    assert runs == ["print(2+2)", "print(1)"] and len(reply.runs) == 2
    second = client.calls[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"] is responses[0].content  # full content kept
    results = second[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert results[0]["content"].startswith("exit code 0") and results[0]["is_error"] is False
    assert reply.text == "It is 4."


def test_failed_run_is_marked_as_error():
    a, client, _ = agent([NS(stop_reason="tool_use", content=[tool("t1", "1/0")]),
                          NS(stop_reason="end_turn", content=[text("Division by zero.")])],
                         runner=lambda c: RunResult(1, "ZeroDivisionError"))
    a.ask("run 1/0")
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_step_limit_stops_runaway_loops():
    loop = [NS(stop_reason="tool_use", content=[tool(f"t{i}", "x")]) for i in range(4)]
    a, client, runs = agent(loop + [NS(stop_reason="end_turn", content=[text("done")])], max_steps=2)
    assert a.ask("go").text == "done"
    assert len(runs) == 2
    assert "Step limit" in client.calls[3]["messages"][-1]["content"][0]["content"]


def test_invalid_tool_input_is_not_executed():
    bad = NS(type="tool_use", id="t1", name="run_python", input={"code": 42})
    a, client, runs = agent([NS(stop_reason="tool_use", content=[bad]), NS(stop_reason="end_turn", content=[text("ok")])])
    a.ask("x")
    assert runs == [] and client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_refusal_pause_and_max_tokens():
    a, _, _ = agent([NS(stop_reason="refusal", content=[])])
    assert a.ask("bad").stop_reason == "refusal"
    a, client, _ = agent([NS(stop_reason="pause_turn", content=[text("…")]),
                          NS(stop_reason="max_tokens", content=[text("long")])])
    reply = a.ask("x")
    assert len(client.calls) == 2 and "cut off" in reply.text


def test_history_is_kept_trimmed_and_valid():
    a, client, _ = agent([NS(stop_reason="end_turn", content=[text(str(i))]) for i in range(30)])
    for i in range(30):
        a.ask(f"q{i}")
    assert len(a.history) <= MAX_HISTORY_MESSAGES
    first = a.history[0]
    assert first["role"] == "user" and isinstance(first["content"], str)
    a.reset()
    assert a.history == []


# --- sandbox -----------------------------------------------------------------------

def test_docker_command_is_locked_down(tmp_path):
    cmd = docker_command("img", str(tmp_path), 20)
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "no-new-privileges", "--pids-limit"):
        assert flag in cmd
    user = cmd[cmd.index("--user") + 1]
    assert not user.startswith("0:") and user != "root"
    assert f"{tmp_path}:/workspace:rw" in cmd
    assert cmd[-3:] == ["python", "-I", "-"]


def test_run_python_paths(tmp_path):
    seen = {}

    def fake(cmd, input, **kw):
        seen["input"] = input
        return NS(returncode=0, stdout="hi\n", stderr="")
    assert run_python("print('hi')", "img", str(tmp_path), 5, runner=fake).output == "hi"
    assert seen["input"] == "print('hi')"  # code goes through stdin, never the command line
    assert run_python("  ", "img", ".", 5).exit_code == 2
    assert run_python("x" * 30000, "img", ".", 5).exit_code == 2
    assert run_python("x", "img", ".", 5, runner=lambda *a, **k: NS(returncode=137, stdout="", stderr="")).timed_out

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("docker", 1)
    assert run_python("x", "img", ".", 5, runner=slow).timed_out


def test_truncate_and_workspace(tmp_path):
    out = truncate("\x1b[31mred\x1b[0m" + "x" * 10000, 100)
    assert "\x1b" not in out and "truncated" in out and len(out) < 150
    path = workspace_for(str(tmp_path), -100123)
    assert path.endswith("chat--100123")


# --- security, config, telegram helpers ----------------------------------------------

def test_access_and_rate_limit():
    assert is_allowed(5, frozenset({5})) and not is_allowed(6, frozenset({5})) and not is_allowed(None, frozenset())
    now = [0.0]
    rl = RateLimiter(2, window=60, clock=lambda: now[0])
    assert rl.allow(1) and rl.allow(1) and not rl.allow(1) and rl.allow(2)
    now[0] = 61
    assert rl.allow(1)
    assert clean_input("  hi ") == "hi" and clean_input("") is None and clean_input("x" * 9000) is None


def test_settings():
    env = {"TELEGRAM_TOKEN": "t", "ALLOWED_USER_ID": "1, 2", "ANTHROPIC_API_KEY": "k"}
    s = load_settings(env)
    assert s.allowed_user_ids == {1, 2} and s.model == "claude-haiku-5-5"
    for bad in ({}, {**env, "ALLOWED_USER_ID": "me"}, {**env, "CLAUDE_EFFORT": "turbo"}):
        with pytest.raises(ValueError):
            load_settings(bad)


def test_chunks():
    parts = chunks("line\n" * 2000 + "y" * 9000)
    assert all(len(p) <= 4000 for p in parts)
    assert "".join(parts) == "line\n" * 2000 + "y" * 9000


# --- robustness ----------------------------------------------------------------------

def thinking(sig="sig"):
    return NS(type="thinking", thinking="", signature=sig)


def test_model_that_ignores_the_step_limit_is_stopped():
    # Claude keeps calling the tool forever: the loop must end, and the stored history
    # must stay valid (every tool_use answered) for the next request.
    endless = [NS(stop_reason="tool_use", content=[tool(f"t{i}", "x")]) for i in range(50)]
    a, client, runs = agent(endless, max_steps=3)
    reply = a.ask("go")
    assert reply.stop_reason == "step_limit" and len(runs) == 3
    assert len(client.calls) <= 3 + 3  # max_steps + EXTRA_ROUNDS_AFTER_LIMIT + 1
    ids = {b.id for m in a.history if m["role"] == "assistant" and not isinstance(m["content"], str)
           for b in m["content"] if getattr(b, "type", None) == "tool_use"}
    answered = {r["tool_use_id"] for m in a.history if m["role"] == "user" and isinstance(m["content"], list)
                for r in m["content"]}
    assert ids <= answered


def test_endless_pause_turn_is_capped():
    a, client, _ = agent([NS(stop_reason="pause_turn", content=[text("…")])] * 20)
    a.ask("x")
    assert len(client.calls) <= 6


def test_history_is_rolled_back_when_a_request_fails():
    class Boom(Exception):
        pass

    responses = [NS(stop_reason="tool_use", content=[tool("t1", "x")])]
    a, client, _ = agent(responses, runner=lambda c: (_ for _ in ()).throw(Boom()))
    a.history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": [text("ok")]}]
    before = list(a.history)
    with pytest.raises(Boom):
        a.ask("run it")
    assert a.history == before  # no dangling tool_use left behind


def test_trim_removes_thinking_blocks_and_stays_valid():
    responses = [NS(stop_reason="end_turn", content=[thinking(), text(str(i))]) for i in range(60)]
    a, client, _ = agent(responses)
    for i in range(60):
        a.ask(f"q{i}")
        assert len(a.history) <= MAX_HISTORY_MESSAGES
    trimmed_once = any(len(c["messages"]) < 2 * i + 1 for i, c in enumerate(client.calls))
    assert trimmed_once
    # After the last trim the remaining (older) assistant turns carry no thinking blocks.
    last = client.calls[-1]["messages"]
    first = last[0]
    assert first["role"] == "user" and isinstance(first["content"], str)


def test_trim_strips_thinking_from_kept_turns():
    a, _, _ = agent([])
    a.history = []
    for i in range(MAX_HISTORY_MESSAGES // 2 + 1):
        a.history.append({"role": "user", "content": f"q{i}"})
        a.history.append({"role": "assistant", "content": [thinking(), text(str(i))]})
    a._trim()
    assert len(a.history) <= MAX_HISTORY_MESSAGES // 2
    for m in a.history:
        if m["role"] == "assistant":
            assert all(b.type != "thinking" for b in m["content"])


def test_settings_numbers_are_validated():
    env = {"TELEGRAM_TOKEN": "t", "ALLOWED_USER_ID": "1", "ANTHROPIC_API_KEY": "k"}
    assert load_settings({**env, "DAEDALUS_SANDBOX_TIMEOUT": "45"}).sandbox_timeout == 45
    for bad in ({"DAEDALUS_SANDBOX_TIMEOUT": "abc"}, {"DAEDALUS_SANDBOX_TIMEOUT": "0"},
                {"DAEDALUS_REQUESTS_PER_HOUR": "-1"}, {"ALLOWED_USER_ID": ","}):
        with pytest.raises(ValueError):
            load_settings({**env, **bad})


def test_killed_run_mentions_both_limits(tmp_path):
    r = run_python("x", "img", str(tmp_path), 5, runner=lambda *a, **k: NS(returncode=137, stdout="", stderr=""))
    assert "memory" in r.output and "time" in r.output


def test_concurrent_messages_in_one_chat_are_serialized(monkeypatch):
    """Two messages at once must not run the same Agent in two threads."""
    import asyncio
    import sys
    import threading
    import types

    pytest.importorskip("telegram")
    from app.handlers import telegram as tg

    active, overlap = [0], [False]
    gate = threading.Lock()

    class SlowAgent:
        def __init__(self, *a, **k):
            pass

        def ask(self, text):
            with gate:
                active[0] += 1
                overlap[0] |= active[0] > 1
            import time
            time.sleep(0.05)
            with gate:
                active[0] -= 1
            return NS(text="ok", runs=[])

        def reset(self):
            pass

    monkeypatch.setattr(tg, "Agent", SlowAgent)
    handlers = {}

    class FakeApp:
        def add_handler(self, h):
            handlers[type(h).__name__ + str(len(handlers))] = h

    class FakeBuilder:
        def token(self, t):
            return self

        def build(self):
            return FakeApp()

    monkeypatch.setattr("telegram.ext.ApplicationBuilder", FakeBuilder)
    settings = load_settings({"TELEGRAM_TOKEN": "t", "ALLOWED_USER_ID": "7", "ANTHROPIC_API_KEY": "k"})
    settings = type(settings)(**{**settings.__dict__, "workspace": str(__import__("tempfile").mkdtemp())})
    tg.build_app(settings, client=None)
    on_text = [h for h in handlers.values() if type(h).__name__ == "MessageHandler"][0].callback

    sent = []

    class Msg:
        def __init__(self, t):
            self.text = t

        async def reply_text(self, t):
            sent.append(t)

    def update(t):
        return NS(effective_message=Msg(t), message=None, effective_user=NS(id=7), effective_chat=NS(id=1))

    async def action(*a, **k):
        pass

    context = NS(bot=NS(send_chat_action=action))

    async def main():
        await asyncio.gather(on_text(update("a"), context), on_text(update("b"), context))
    asyncio.run(main())
    assert not overlap[0]
    assert sent.count("ok") == 2


def test_output_flood_is_capped_and_the_container_stopped():
    import sys
    from app.sandbox import MAX_CAPTURE, capped_run
    killed = []
    flood = "import sys\nwhile True: sys.stdout.write('x' * 100000)"
    r = capped_run([sys.executable, "-c", flood], "", 20, container="daedalus-run-t", kill=killed.append)
    assert r.overflow and len(r.stdout) == MAX_CAPTURE and killed == ["daedalus-run-t"]


def test_capped_run_passes_stdin_and_keeps_both_streams():
    import sys
    from app.sandbox import capped_run
    code = "import sys\nprint(sys.stdin.read().upper())\nprint('warn', file=sys.stderr)\nsys.exit(3)"
    r = capped_run([sys.executable, "-c", code], "hello", 10, kill=lambda name: None)
    assert (r.returncode, r.stdout.strip(), r.stderr.strip(), r.overflow) == (3, "HELLO", "warn", False)


def test_capped_run_timeout_stops_the_container():
    import sys
    import time
    from app.sandbox import capped_run
    killed = []
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        capped_run([sys.executable, "-c", "import time; time.sleep(30)"], "", 0.5, container="c", kill=killed.append)
    assert killed == ["c"] and time.monotonic() - started < 5


def test_flood_message_and_named_container(tmp_path):
    from app.sandbox import Captured
    cmd = docker_command("img", str(tmp_path), 20, name="daedalus-run-ab")
    assert cmd[cmd.index("--name") + 1] == "daedalus-run-ab" and "--name" not in docker_command("img", ".", 20)
    r = run_python("x", "img", str(tmp_path), 5, runner=lambda *a, **k: Captured(137, "xxx", "", overflow=True))
    assert "[stopped: the code printed more than 64 kB]" in r.output and not r.timed_out
