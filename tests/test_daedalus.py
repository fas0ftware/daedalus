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
