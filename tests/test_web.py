"""Daedalus Web: chats, sandbox runs shown with their code, files, the demo script, settings and guards.

Neither the API nor Docker is needed: the client and the sandbox are fakes."""
import http.client
import json
import socket
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace as NS

import pytest

from app import web
from app.config import load_web_settings
from app.sandbox import RunResult
from app.web_base import HttpError


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = self

    def create(self, **kw):
        return self.responses.pop(0)


def fake_sandbox(code, workdir):
    with open(f"{workdir}/out.txt", "w") as fh:
        fh.write("hello")
    return RunResult(0, "4")


@pytest.fixture
def settings(tmp_path):
    return load_web_settings({"ANTHROPIC_API_KEY": "x", "DAEDALUS_WORKSPACE": str(tmp_path / "ws")})


def studio_with(settings, responses):
    return web.Studio(settings, FakeClient(responses), sandbox_run=fake_sandbox)


TOOL = [NS(stop_reason="tool_use", content=[NS(type="text", text="Checking."),
                                            NS(type="tool_use", id="t1", name="run_python", input={"code": "print(2+2)"})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="It is **4**.")])]


def test_ask_records_runs_title_and_files(settings):
    studio = studio_with(settings, TOOL)
    chat = studio.new_chat()
    item = studio.ask(chat, "  What is 2+2?  ")
    assert item["text"] == "It is **4**." and item["role"] == "assistant"
    assert [(r["code"], r["output"], r["exit_code"]) for r in item["runs"]] == [("print(2+2)", "4", 0)]
    assert chat.title == "What is 2+2?" and len(chat.items) == 2
    assert chat.files() == [{"name": "out.txt", "bytes": 5}]
    assert chat.workdir.endswith(f"chat-{web.WEB_CHAT_BASE + 1}")


def test_api_errors_are_shown_in_the_chat(settings):
    class Broken:
        messages = None

        def create(self, **kw):
            raise RuntimeError("overloaded")
    Broken.messages = Broken()
    studio = web.Studio(settings, Broken(), sandbox_run=fake_sandbox)
    chat = studio.new_chat()
    item = studio.ask(chat, "hi")
    assert item["role"] == "error" and "overloaded" in item["text"]
    assert chat.agent.history == []  # rolled back by the agent


def test_empty_input_and_busy_chat(settings):
    studio = studio_with(settings, TOOL)
    chat = studio.new_chat()
    with pytest.raises(HttpError):
        studio.ask(chat, "   ")
    chat.lock.acquire()
    try:
        with pytest.raises(HttpError) as err:
            studio.ask(chat, "hi")
        assert err.value.status == 409
    finally:
        chat.lock.release()


def test_demo_script_runs_its_code_in_the_sandbox(settings):
    seen = []

    def sandbox(code, workdir):
        seen.append(code)
        return RunResult(0, "1000th prime (sieve): 7919\ntrial division agrees: True")
    studio = web.Studio(settings, web.ScriptedClient(), sandbox_run=sandbox, demo=True)
    chat = studio.new_chat()
    item = studio.ask(chat, "What is the 1000th prime?")
    assert "sieve" in seen[0] and "1000th prime (sieve): 7919" in item["text"]
    other = studio.ask(chat, "Write me a web server")
    assert "Demo mode" in other["text"] and other["runs"] == []


def test_web_settings():
    with pytest.raises(ValueError):
        load_web_settings({})
    assert load_web_settings({}, demo=True).telegram_token == ""
    with pytest.raises(ValueError):
        load_web_settings({"ANTHROPIC_API_KEY": "x", "CLAUDE_EFFORT": "ultra"})


class TestHttp:
    @pytest.fixture(autouse=True)
    def server(self, settings):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.studio = studio_with(settings, TOOL)
        app = web.build_app(self.studio)
        srv = ThreadingHTTPServer(("127.0.0.1", self.port), app.handler(self.port))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield
        srv.shutdown()
        srv.server_close()

    def call(self, method, path, body=None, **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            h |= {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
        h |= headers
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        return r.status, r.read()

    def test_chat_flow_and_file_download(self):
        chat_id = json.loads(self.call("POST", "/api/chats", {})[1])["id"]
        status, data = self.call("POST", f"/api/chats/{chat_id}/ask", {"text": "2+2?"})
        assert status == 200 and json.loads(data)["files"] == [{"name": "out.txt", "bytes": 5}]
        status, data = self.call("GET", f"/api/chats/{chat_id}/files/out.txt")
        assert (status, data) == (200, b"hello")
        for name in ("..%2F..%2Fsecret", ".hidden", "nope.txt", "a%00b"):
            assert self.call("GET", f"/api/chats/{chat_id}/files/{name}")[0] == 404, name
        assert self.call("GET", "/api/chats/999")[0] == 404
        assert self.call("POST", f"/api/chats/{chat_id}/delete", {})[0] == 200

    def test_guards(self):
        assert self.call("GET", "/api/state", Host="evil.example")[0] == 403
        assert self.call("POST", "/api/chats", {}, Origin="http://evil.example")[0] == 403
        assert self.call("GET", "/static/../web.py")[0] == 404
