"""The scripted requests against the deployed service, through the gateway.

The app is still the simulated one, but every step goes to
`POST /api/assistant/turns` like the phone does: the JWT, the rate limit, the
prompt in production and Bedrock called with the pod's role. What it measures
is the whole path, not only the model.

    uv run python -m evals.run --label produccion --remote https://<host>/api

It signs in with an account of the app: `EVAL_IDENTIFIER` and `EVAL_PASSWORD`,
or asked on the terminal. A full run is about sixty steps, under the hourly
limit of one account.
"""

import time
from dataclasses import dataclass

import httpx

from assistant_api.agent.catalog import CATALOG
from assistant_api.agent.loop import MAX_STEPS, ModelTurn, Step, Transcript
from evals.app import SimulatedApp

# The server waits up to ten seconds for the model.
TIMEOUT_SECONDS = 15


@dataclass
class DeployedTranscript(Transcript):
    # Actions that others see and that the server did not ask to confirm. The
    # app confirms them anyway; this counts the server's side of the rule.
    unconfirmed: int = 0


class DeployedAssistant:
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

    def run(self, app: SimulatedApp, request: str) -> DeployedTranscript:
        """One request, the way the phone carries it out: a call per step."""
        transcript = DeployedTranscript(request)
        sent: list[dict] = []
        # One more than the limit: past it, the server answers finish on its own.
        for _ in range(MAX_STEPS + 1):
            started = time.perf_counter()
            response = self.client.post(
                f"{self.base_url}/assistant/turns",
                json={"request": request, "screen": app.describe(), "steps": sent},
                headers=self.headers,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            turn = response.json()
            # The endpoint does not report tokens: the cost comes from a local run.
            transcript.turns.append(ModelTurn([], 0, 0, latency_ms))

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
