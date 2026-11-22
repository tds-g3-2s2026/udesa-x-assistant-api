"""The scripted requests the way the phone carries them out: one call per step.

The app is still the simulated one, but every step is answered like
`POST /api/assistant/turns` answers it: with the request, the screen and the
steps already taken, and the conversation rebuilt from those on every call.

Against the deployed service, through the gateway, it measures the whole path:
the JWT, the rate limit, the prompt in production and Bedrock called with the
pod's role.

    uv run python -m evals.run --label produccion --remote https://<host>/api

It signs in with an account of the app: `EVAL_IDENTIFIER` and `EVAL_PASSWORD`,
or asked on the terminal. A full run is about sixty steps, under the hourly
limit of one account.

Locally, the same steps go to `decide` in this process, the function behind the
endpoint. The loop of `agent/loop.py` keeps the whole conversation instead, and
the model does not always answer the same in both: on 20/11 it followed one of
two accounts and wrote a reply nobody dictated, only on the endpoint's path.
"""

import time
from dataclasses import dataclass

import httpx

from assistant_api.agent.catalog import CATALOG
from assistant_api.agent.loop import MAX_STEPS, Model, ModelTurn, Step, Transcript
from assistant_api.agent.turn import PastStep, decide
from evals.app import SimulatedApp

# The server waits up to ten seconds for the model.
TIMEOUT_SECONDS = 15


@dataclass
class DeployedTranscript(Transcript):
    # Actions that others see and that the server did not ask to confirm. The
    # app confirms them anyway; this counts the server's side of the rule.
    unconfirmed: int = 0


class StepByStep:
    """Carries out a request like the phone: asks for a step, performs it, sends it back."""

    def step(self, request: str, screen: dict, steps: list[dict]) -> tuple[dict, list[ModelTurn]]:
        """The endpoint's answer for the next step, and the model calls behind it."""
        raise NotImplementedError

    def run(self, app: SimulatedApp, request: str) -> DeployedTranscript:
        transcript = DeployedTranscript(request)
        sent: list[dict] = []
        # One more than the limit: past it, the server answers finish on its own.
        for _ in range(MAX_STEPS + 1):
            turn, model_turns = self.step(request, app.describe(), sent)
            transcript.turns.extend(model_turns)

            name, arguments = turn["action"], turn["arguments"]
            if turn["finished"]:
                transcript.steps.append(Step(name, arguments, {"ok": True}))
                transcript.said = arguments.get("say")
                transcript.outcome = "finished"
                return transcript
            if CATALOG[name].needs_confirmation and not turn["needsConfirmation"]:
                transcript.unconfirmed += 1
            result = app.perform(name, arguments)
            transcript.steps.append(Step(name, arguments, result))
            sent.append({"action": name, "arguments": arguments, "result": result})
        return transcript


class DeployedAssistant(StepByStep):
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        self.headers = {"Authorization": f"Bearer {token}"}

    @classmethod
    def sign_in(
        cls, base_url: str, identifier: str, password: str, client: httpx.Client | None = None
    ) -> "DeployedAssistant":
        client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        response = client.post(
            f"{base_url.rstrip('/')}/auth/login",
            json={"identifier": identifier, "password": password},
        )
        response.raise_for_status()
        return cls(base_url, response.json()["access_token"], client)

    def step(self, request: str, screen: dict, steps: list[dict]) -> tuple[dict, list[ModelTurn]]:
        started = time.perf_counter()
        response = self.client.post(
            f"{self.base_url}/assistant/turns",
            json={"request": request, "screen": screen, "steps": steps},
            headers=self.headers,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        # The endpoint does not report tokens: the cost comes from a local run.
        return response.json(), [ModelTurn([], 0, 0, latency_ms)]


class LocalAssistant(StepByStep):
    """The endpoint's path without the network, with the model from the environment."""

    def __init__(self, model: Model) -> None:
        self.model = model

    def step(self, request: str, screen: dict, steps: list[dict]) -> tuple[dict, list[ModelTurn]]:
        past = [PastStep(step["action"], step["arguments"], step["result"]) for step in steps]
        decision = decide(self.model, request, screen, past)
        turn = {
            "action": decision.action,
            "arguments": decision.arguments,
            "needsConfirmation": decision.needs_confirmation,
            "finished": decision.action == "finish",
        }
        return turn, decision.turns
