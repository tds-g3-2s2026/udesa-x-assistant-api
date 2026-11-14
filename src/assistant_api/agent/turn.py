"""One step of a spoken request, answered without keeping anything between calls.

The app sends what the user asked, the screen it shows now and the steps it
already took. The conversation for the model is rebuilt from those on every
call: the app reports what happened, and never gets to write the model's lines.
Nothing of it is stored (ADR-023).
"""

from dataclasses import dataclass, field

from assistant_api.agent.catalog import CATALOG, invalid_arguments, tool_specs
from assistant_api.agent.loop import MAX_STEPS, Model, ModelTurn, as_json, tool_result, user_text
from assistant_api.agent.prompt import SYSTEM_PROMPT

STEP_LIMIT_SAY = "Llegué al máximo de pasos para un pedido. Si falta algo, pedímelo de nuevo."
NOT_DONE_SAY = "No pude hacerlo. Probá pedírmelo de otra forma."
# A refused action is explained to the model and it gets one more chance in the
# same call; a second refusal ends the request instead of spending more.
ATTEMPTS = 2


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
    """The action the app takes next, or finish with what it reads aloud."""
    if len(steps) >= MAX_STEPS:
        return finish(STEP_LIMIT_SAY, "step_limit", [])

    messages = conversation(request, screen, steps)
    turns: list[ModelTurn] = []
    for _ in range(ATTEMPTS):
        turn = model.converse(SYSTEM_PROMPT, messages, tool_specs())
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
