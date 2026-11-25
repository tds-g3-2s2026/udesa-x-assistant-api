"""The evaluation against the deployed service, with a fake server: no request leaves the test."""

import argparse
import json

import httpx
import pytest

from assistant_api.agent.loop import ModelTurn
from evals import run
from evals.app import SimulatedApp
from evals.remote import DeployedAssistant, LocalAssistant

BASE = "https://udesa-x.test/api"


def turn(action: str, arguments: dict, *, confirm: bool = False, finished: bool = False) -> dict:
    return {
        "action": action,
        "arguments": arguments,
        "needsConfirmation": confirm,
        "finished": finished,
    }


def fake_server(*turns: dict):
    """Answers the login and then each step with the next scripted turn."""
    received: list[httpx.Request] = []
    pending = list(turns)

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request)
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "token-de-prueba"})
        return httpx.Response(200, json=pending.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handle)), received


def test_ai06_a_request_goes_through_the_deployed_endpoint_step_by_step():
    client, received = fake_server(
        turn("like_post", {"ref": "p3"}), turn("finish", {"say": "Listo"}, finished=True)
    )
    assistant = DeployedAssistant.sign_in(BASE, "@alumno", "clave", client)
    app = SimulatedApp()

    transcript = assistant.run(app, "dale like al tercero")

    assert (transcript.outcome, transcript.said) == ("finished", "Listo")
    assert app.changes()["like"] == ["post-03"]
    login, first, second = received
    assert json.loads(login.content) == {"identifier": "@alumno", "password": "clave"}
    assert first.headers["Authorization"] == "Bearer token-de-prueba"
    assert json.loads(first.content)["steps"] == []
    sent = json.loads(second.content)
    assert sent["request"] == "dale like al tercero"
    assert sent["steps"] == [
        {
            "action": "like_post",
            "arguments": {"ref": "p3"},
            "result": {"ok": True, "done": "Like dado"},
        }
    ]
    assert len(transcript.turns) == 2


def test_ai06_a_visible_action_the_server_did_not_mark_is_counted():
    client, _ = fake_server(
        turn("retweet_post", {"ref": "p1"}),
        turn("follow", {"ref": "a1"}, confirm=True),
        turn("finish", {"say": "Listo"}, finished=True),
    )
    transcript = DeployedAssistant(BASE, "token", client).run(SimulatedApp(), "retuiteá")

    assert transcript.unconfirmed == 1


def test_a_server_that_never_finishes_runs_out_of_steps():
    client, received = fake_server(*[turn("refresh_feed", {})] * 9)

    transcript = DeployedAssistant(BASE, "token", client).run(SimulatedApp(), "actualizá")

    assert transcript.outcome == "step_limit"
    assert len(received) == 9


def test_a_refused_login_stops_the_evaluation():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401)))

    with pytest.raises(httpx.HTTPStatusError):
        DeployedAssistant.sign_in(BASE, "@alumno", "mal", client)


def test_remote_runs_sign_in_with_the_account_from_the_environment(monkeypatch):
    signed: list[tuple] = []

    def sign_in(url, identifier, password):
        signed.append((url, identifier, password))
        return DeployedAssistant(url, "token", fake_server()[0])

    monkeypatch.setenv("EVAL_IDENTIFIER", "@alumno")
    monkeypatch.setenv("EVAL_PASSWORD", "clave")
    monkeypatch.setattr(DeployedAssistant, "sign_in", staticmethod(sign_in))

    runner = run.runner_for(argparse.Namespace(remote=BASE, thinking=False))

    assert signed == [(BASE, "@alumno", "clave")]
    assert callable(runner)


class ScriptedModel:
    """Answers each call with the next scripted action, and keeps what it was sent."""

    name = "scripted"
    prices = (0.0, 0.0)

    def __init__(self, *actions: tuple[str, dict]) -> None:
        self.actions = list(actions)
        self.received: list[list[dict]] = []

    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.received.append(list(messages))
        name, arguments = self.actions.pop(0)
        call = {"toolUse": {"toolUseId": "t1", "name": name, "input": arguments}}
        return ModelTurn(content=[call], input_tokens=100, output_tokens=10, latency_ms=50.0)


def test_ai06_a_local_run_rebuilds_the_conversation_like_the_endpoint():
    model = ScriptedModel(("like_post", {"ref": "p3"}), ("finish", {"say": "Listo"}))
    app = SimulatedApp()

    transcript = LocalAssistant(model).run(app, "dale like al tercero")

    assert (transcript.outcome, transcript.said) == ("finished", "Listo")
    assert app.changes()["like"] == ["post-03"]
    assert transcript.input_tokens == 200
    # The second call starts again from the request, as turns.py does, and the
    # screen travels only with the last result.
    first, *rest = model.received[1]
    assert first["content"][0]["text"] == "Pedido: dale like al tercero"
    assert "pantalla" in rest[-1]["content"][0]["toolResult"]["content"][0]["json"]


def test_local_runs_use_the_model_from_the_environment(monkeypatch):
    built: list[bool] = []
    monkeypatch.setattr(run, "model_from_environment", lambda thinking: built.append(thinking))

    run.runner_for(argparse.Namespace(remote=None, thinking=True))

    assert built == [True]
