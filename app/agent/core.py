"""The coding agent: Claude + one client-side tool, `run_python`, executed in the Docker sandbox."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..sandbox import RunResult

SYSTEM = (
    "You are Daedalus, a personal coding assistant reached through Telegram. "
    "Write clear, working code. When running code would confirm an answer (calculations, "
    "data checks, testing a snippet), use the run_python tool: it executes Python 3.12 in an "
    "isolated container with no network access; files written to the current folder persist "
    "for this chat. Keep replies short enough for a phone screen and put code in fenced blocks."
)

RUN_PYTHON_TOOL = {
    "name": "run_python",
    "description": ("Run a Python 3.12 script in an isolated sandbox (no network, 256 MB RAM, "
                    "about 20 s). Returns stdout, stderr and the exit code. The working folder "
                    "persists between runs in the same chat."),
    "input_schema": {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Complete Python source to run"}},
        "required": ["code"],
        "additionalProperties": False,
    },
    "strict": True,
}

MAX_HISTORY_MESSAGES = 40
# After a trim the history is cut to about half, so trims (which break the prompt
# cache and drop thinking blocks) happen rarely instead of on every turn.
TRIM_TARGET = MAX_HISTORY_MESSAGES // 2
# If Claude keeps calling tools after the step limit was announced, give up after
# this many extra rounds instead of looping (and paying) forever.
EXTRA_ROUNDS_AFTER_LIMIT = 2
MAX_PAUSES = 5
THINKING_TYPES = ("thinking", "redacted_thinking")


@dataclass
class Reply:
    text: str
    runs: list[RunResult] = field(default_factory=list)
    stop_reason: str = ""


class Agent:
    def __init__(self, client, model: str, run_code, effort: str = "medium", max_steps: int = 8):
        """run_code(code: str) -> RunResult; injected so tests need neither Docker nor the API."""
        self.client, self.model, self.run_code = client, model, run_code
        self.effort, self.max_steps = effort, max_steps
        self.history: list[dict] = []

    def reset(self) -> None:
        self.history = []

    def _trim(self) -> None:
        """Keep the history bounded. Called only between requests, never inside a tool loop.

        The oldest exchanges are dropped, always restarting at a plain user text turn so no
        tool_result is left without its tool_use. Dropping turns edits the history, and newer
        models reject thinking blocks replayed after such an edit, so the thinking blocks of the
        turns that are kept are removed as well (allowed outside an active tool loop).
        """
        if len(self.history) <= MAX_HISTORY_MESSAGES:
            return
        while len(self.history) > TRIM_TARGET:
            self.history.pop(0)
            while self.history and not (self.history[0]["role"] == "user"
                                        and isinstance(self.history[0]["content"], str)):
                self.history.pop(0)
        for message in self.history:
            if message["role"] == "assistant" and not isinstance(message["content"], str):
                kept = [b for b in message["content"] if _block_type(b) not in THINKING_TYPES]
                message["content"] = kept or [{"type": "text", "text": "(no answer)"}]

    def ask(self, text: str) -> Reply:
        """One user turn. On any error the history is restored, so a failed API call or a
        crashed sandbox never leaves a dangling tool_use that would break every later request."""
        checkpoint = len(self.history)
        try:
            return self._ask(text)
        except BaseException:
            del self.history[checkpoint:]
            raise

    def _ask(self, text: str) -> Reply:
        self.history.append({"role": "user", "content": text})
        runs: list[RunResult] = []
        steps = 0
        pauses = 0
        while True:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                tools=[RUN_PYTHON_TOOL],
                output_config={"effort": self.effort},
                messages=self.history,
            )
            # Keep the full content (thinking and tool_use blocks) so the next request is valid.
            self.history.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                self._trim()
                return Reply("I can't help with that request.", runs, "refusal")
            if response.stop_reason == "pause_turn" and pauses < MAX_PAUSES:
                pauses += 1
                continue
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break

            steps += 1
            if steps > self.max_steps + EXTRA_ROUNDS_AFTER_LIMIT:
                # Claude ignored the step limit; stop here. The unanswered tool calls are
                # answered so the stored history stays valid for the next request.
                self.history.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                     "content": "Step limit reached."} for b in tool_uses]})
                self.history.append({"role": "assistant", "content": [
                    {"type": "text", "text": "(stopped: too many sandbox runs)"}]})
                self._trim()
                partial = "\n".join(b.text for b in response.content if b.type == "text").strip()
                return Reply((partial + "\n\n" if partial else "")
                             + f"(stopped after {len(runs)} sandbox runs; ask again to continue)",
                             runs, "step_limit")
            results = []
            for block in tool_uses:
                if steps > self.max_steps:
                    content, is_error = "Step limit reached; answer with what you have.", True
                elif block.name != "run_python" or not isinstance(block.input, dict) \
                        or not isinstance(block.input.get("code"), str):
                    content, is_error = "Unknown tool or invalid input.", True
                else:
                    result = self.run_code(block.input["code"])
                    runs.append(result)
                    content = f"exit code {result.exit_code}\n{result.output}"
                    is_error = result.exit_code != 0
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content,
                                "is_error": is_error})
            self.history.append({"role": "user", "content": results})  # all results in one message

        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text += "\n\n(answer cut off: too long)"
        self._trim()
        return Reply(text or "(no answer)", runs, response.stop_reason)


def _block_type(block) -> str:
    return block.get("type", "") if isinstance(block, dict) else getattr(block, "type", "")
