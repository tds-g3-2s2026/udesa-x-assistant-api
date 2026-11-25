"""One step of a spoken request, answered without keeping anything between calls.

The app sends what the user asked, the screen it shows now and the steps it
already took. The conversation for the model is rebuilt from those on every
call: the app reports what happened, and never gets to write the model's lines.
The service stores none of it (ADR-023). The traces of Langfuse carry it, for
the team and the tutor to review (ADR-027).
"""

from dataclasses import asdict, dataclass, field

from assistant_api import telemetry
from assistant_api.agent.catalog import CATALOG, invalid_arguments, tool_specs
from assistant_api.agent.loop import MAX_STEPS, Model, ModelTurn, as_json, tool_result, user_text
from assistant_api.agent.prompt import SYSTEM_PROMPT

STEP_LIMIT_SAY = "Llegué al máximo de pasos para un pedido. Si falta algo, pedímelo de nuevo."
NOT_DONE_SAY = "No pude hacerlo. Probá pedírmelo de otra forma."
# A refused action is explained to the model and it gets one more chance in the
# same call; a second refusal ends the request instead of spending more.
ATTEMPTS = 2
# The outcomes where the model answered with something the app could use.
ANSWERED = ("action", "finished")


@dataclass(frozen=True)
class PastStep:
    action: str
    arguments: dict
    # What the app answered when it performed the action.
    result: dict


@dataclass(frozen=True)
class Decision:
    action: str
    arguments: dict
    needs_confirmation: bool
    # action: an action for the app. finished: the model called finish.
    # step_limit, no_action and refused: the server ended the request.
    outcome: str
    turns: list[ModelTurn] = field(default_factory=list)


def screen_refs(screen: object) -> set[str]:
    """Every `ref` on the screen the app sent, however deep it is."""
    if isinstance(screen, dict):
        found = {screen["ref"]} if isinstance(screen.get("ref"), str) else set()
        return found.union(*(screen_refs(value) for value in screen.values()))
    if isinstance(screen, list):
        return set().union(*(screen_refs(value) for value in screen))
    return set()


def refusal(name: str, arguments: dict, screen: dict) -> str | None:
    """Why the action cannot go to the app, or None when it can.

    On top of the catalog check, the reference has to be on the screen the app
    sent: the model cannot point at a post the user is not looking at.
    """
    problem = invalid_arguments(name, arguments)
    if problem:
        return problem
    ref = arguments.get("ref")
    if ref is not None and ref not in screen_refs(screen):
        return f"{ref} no está en la pantalla"
    return None


def conversation(request: str, screen: dict, steps: list[PastStep]) -> list[dict]:
    """The messages of the request so far, rebuilt from what the app reports.

    The current screen goes with the last result, the same place the loop of
    the evaluation puts it; on the first step, with the request.
    """
    if not steps:
        return [user_text(f"Pedido: {request}\n\nPantalla:\n{as_json(screen)}")]
    messages = [user_text(f"Pedido: {request}")]
    for number, step in enumerate(steps, start=1):
        tool_use_id = f"paso{number}"
        call = {"toolUseId": tool_use_id, "name": step.action, "input": step.arguments}
        messages.append({"role": "assistant", "content": [{"toolUse": call}]})
        result = {**step.result, "pantalla": screen} if number == len(steps) else step.result
        messages.append({"role": "user", "content": [tool_result(tool_use_id, result)]})
    return messages


def finish(say: str, outcome: str, turns: list[ModelTurn]) -> Decision:
    return Decision("finish", {"say": say}, False, outcome, turns)


def next_step(model: Model, request: str, screen: dict, steps: list[PastStep]) -> Decision:
    """The action the app takes next, or finish with what it reads aloud.

    The step is a span: Grafana sees its action and outcome, and Langfuse also
    what the app sent and what went back to it (ADR-027).
    """
    with telemetry.agent_tracer.start_as_current_span(telemetry.STEP_SPAN) as span:
        span.set_attributes(
            {
                "langfuse.observation.type": "agent",
                "langfuse.observation.metadata.step": len(steps) + 1,
                "langfuse.observation.input": as_json(
                    {"pedido": request, "pantalla": screen, "pasos": [asdict(s) for s in steps]}
                ),
            }
        )
        decision = decide(model, request, screen, steps)
        # Ended by the server: worth a look when reviewing the traces.
        level = "DEFAULT" if decision.outcome in ANSWERED else "WARNING"
        span.set_attributes(
            {
                "langfuse.observation.metadata.action": decision.action,
                "langfuse.observation.metadata.outcome": decision.outcome,
                "langfuse.observation.level": level,
                "langfuse.observation.output": as_json(
                    {"action": decision.action, "arguments": decision.arguments}
                ),
            }
        )
    record(decision)
    return decision


def cost(model: Model, turn: ModelTurn) -> float:
    """USD of one call, with the model's prices per million tokens."""
    input_price, output_price = model.prices
    return (turn.input_tokens * input_price + turn.output_tokens * output_price) / 1_000_000


def record(decision: Decision) -> None:
    """What the step cost and how it ended. Only catalog names and numbers: no text."""
    telemetry.steps.add(1, {"action": decision.action, "outcome": decision.outcome})
    for turn in decision.turns:
        telemetry.model_duration.record(turn.latency_ms / 1000)
        telemetry.model_tokens.add(turn.input_tokens, {"type": "input"})
        telemetry.model_tokens.add(turn.output_tokens, {"type": "output"})
    if decision.action == "finish":
        telemetry.spoken_length.record(len(decision.arguments.get("say", "")))


def decide(model: Model, request: str, screen: dict, steps: list[PastStep]) -> Decision:
    if len(steps) >= MAX_STEPS:
        return finish(STEP_LIMIT_SAY, "step_limit", [])

    messages = conversation(request, screen, steps)
    turns: list[ModelTurn] = []
    for _ in range(ATTEMPTS):
        # A span per call, inside the step's: Grafana gets the tokens and the
        # cost, and Langfuse also the messages and the answer.
        with telemetry.agent_tracer.start_as_current_span(telemetry.MODEL_SPAN) as span:
            span.set_attributes(
                {
                    "langfuse.observation.type": "generation",
                    "gen_ai.system": "aws.bedrock",
                    "gen_ai.request.model": model.name,
                    "langfuse.observation.input": as_json(
                        [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
                    ),
                }
            )
            turn = model.converse(SYSTEM_PROMPT, messages, tool_specs())
            span.set_attributes(
                {
                    "gen_ai.usage.input_tokens": turn.input_tokens,
                    "gen_ai.usage.output_tokens": turn.output_tokens,
                    "gen_ai.usage.cost": cost(model, turn),
                    "langfuse.observation.output": as_json(turn.content),
                }
            )
        turns.append(turn)
        calls = [block["toolUse"] for block in turn.content if "toolUse" in block]
        if not calls:
            said = " ".join(block["text"] for block in turn.content if "text" in block)
            return finish(said.strip() or NOT_DONE_SAY, "no_action", turns)

        # Only the first action of a turn goes to the app: one per step.
        call = calls[0]
        name, arguments = call["name"], call.get("input") or {}
        problem = refusal(name, arguments, screen)
        if problem is None:
            if name == "finish":
                return Decision(name, arguments, False, "finished", turns)
            return Decision(name, arguments, CATALOG[name].needs_confirmation, "action", turns)

        messages = [
            *messages,
            {"role": "assistant", "content": [{"toolUse": call}]},
            {
                "role": "user",
                "content": [tool_result(call["toolUseId"], {"ok": False, "error": problem})],
            },
        ]
    return finish(NOT_DONE_SAY, "refused", turns)
