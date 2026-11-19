"""The evaluation against the deployed service, with a fake server: no request leaves the test."""

import argparse
import json

import httpx
import pytest

from evals import run
from evals.app import SimulatedApp
from evals.remote import DeployedAssistant

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


def test_local_runs_use_the_model_from_the_environment(monkeypatch):
    built: list[bool] = []
    monkeypatch.setattr(run, "model_from_environment", lambda thinking: built.append(thinking))

    run.runner_for(argparse.Namespace(remote=None, thinking=True))

    assert built == [True]
