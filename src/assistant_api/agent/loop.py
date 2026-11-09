"""The agent loop: the model picks one action, the app performs it, and the model
sees the new screen, until it calls `finish` or runs out of steps.

The model and the app are both parameters. The evaluation passes Bedrock and a
simulated app; the tests pass a scripted model that never calls Bedrock.
"""

import json
from dataclasses import dataclass, field
from typing import Protocol

from assistant_api.agent.catalog import invalid_arguments, tool_specs
from assistant_api.agent.prompt import SYSTEM_PROMPT

# A request that takes more than this is a request the user already gave up on.
MAX_STEPS = 8

ONE_ACTION_PER_STEP = "Una acción por paso: esta no se ejecutó"


@dataclass(frozen=True)
class ModelTurn:
    """One answer of the model, as Converse returns it."""

    content: list[dict]
    input_tokens: int
    output_tokens: int
    latency_ms: float


class Model(Protocol):
    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn: ...


class App(Protocol):
    """What the app shows and what it does when the assistant taps."""

    def describe(self) -> dict: ...

    def perform(self, name: str, arguments: dict) -> dict: ...


@dataclass(frozen=True)
class Step:
    action: str
    arguments: dict
    # What the app answered, or why the action never reached it.
    result: dict


@dataclass
class Transcript:
    request: str
    steps: list[Step] = field(default_factory=list)
    turns: list[ModelTurn] = field(default_factory=list)
    said: str | None = None
    # finished: it called finish. step_limit: it ran out of steps.
    # no_action: it answered with text instead of a tool.
    outcome: str = "step_limit"

    @property
    def input_tokens(self) -> int:
        return sum(turn.input_tokens for turn in self.turns)

    @property
    def output_tokens(self) -> int:
        return sum(turn.output_tokens for turn in self.turns)


def run_request(model: Model, app: App, request: str, *, max_steps: int = MAX_STEPS) -> Transcript:
    """Carry out one spoken request on the app, one action per model turn."""
    transcript = Transcript(request)
    tools = tool_specs()
    messages = [user_text(f"Pedido: {request}\n\nPantalla:\n{as_json(app.describe())}")]

    for _ in range(max_steps):
        turn = model.converse(SYSTEM_PROMPT, messages, tools)
        transcript.turns.append(turn)
        messages.append({"role": "assistant", "content": turn.content})

        calls = [block["toolUse"] for block in turn.content if "toolUse" in block]
        if not calls:
            transcript.outcome = "no_action"
            transcript.said = " ".join(block["text"] for block in turn.content if "text" in block)
            return transcript

        call, *extra = calls
        name, arguments = call["name"], call.get("input") or {}
        problem = invalid_arguments(name, arguments)
        if problem is None and name == "finish":
            transcript.steps.append(Step(name, arguments, {"ok": True}))
            transcript.said = arguments["say"]
            transcript.outcome = "finished"
            return transcript

        result = {"ok": False, "error": problem} if problem else app.perform(name, arguments)
        transcript.steps.append(Step(name, arguments, result))
        answers = [tool_result(call["toolUseId"], {**result, "pantalla": app.describe()})]
        # Converse wants an answer for every tool the model called, run or not.
        answers += [
            tool_result(other["toolUseId"], {"ok": False, "error": ONE_ACTION_PER_STEP})
            for other in extra
        ]
        messages.append({"role": "user", "content": answers})

    return transcript


def as_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def user_text(text: str) -> dict:
    return {"role": "user", "content": [{"text": text}]}


def tool_result(tool_use_id: str, result: dict) -> dict:
    answer = {"toolUseId": tool_use_id, "content": [{"json": result}]}
    if not result.get("ok", False):
        answer["status"] = "error"
    return {"toolResult": answer}
