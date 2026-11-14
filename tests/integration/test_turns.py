"""`POST /api/assistant/turns` against a scripted model: no call ever reaches Bedrock."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from assistant_api.agent.bedrock import BedrockModel
from assistant_api.agent.loop import MAX_STEPS, ModelTurn
from assistant_api.agent.turn import NOT_DONE_SAY, STEP_LIMIT_SAY
from assistant_api.guards import load_public_key
from assistant_api.main import app
from assistant_api.settings import get_settings
from assistant_api.turns import get_model

PRIVATE_KEY = Ed25519PrivateKey.generate()
PUBLIC_PEM = (
    PRIVATE_KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)

SCREEN = {
    "screen": "feed",
    "posts": [
        {"ref": "p1", "author": "@ana", "text": "Mañana hay parcial de Taller"},
        {"ref": "p2", "author": "@fede", "text": "Alguien juega al fútbol el sábado?"},
    ],
}
URL = "/api/assistant/turns"


def tool_use(name: str, arguments: dict, tool_use_id: str = "t1") -> dict:
    return {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": arguments}}


class ScriptedModel:
    """Answers each call with the next scripted content, and keeps what it was sent."""

    def __init__(self, *turns: list[dict]) -> None:
        self.turns = list(turns)
        self.received: list[list[dict]] = []

    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.received.append(list(messages))
        return ModelTurn(
            content=self.turns.pop(0), input_tokens=900, output_tokens=30, latency_ms=1
        )


def token_for(user_id: uuid.UUID | None = None, *, role: str = "user", key=PRIVATE_KEY) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "users-api",
        "sub": str(user_id or uuid.uuid4()),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, key, algorithm="EdDSA")


def signed_in(user_id: uuid.UUID | None = None, **kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(user_id, **kwargs)}"}


@pytest.fixture
async def api(monkeypatch):
    """The service running, with an in-memory Redis and no model until a test sets one."""
    monkeypatch.setenv("JWT_PUBLIC_KEY", PUBLIC_PEM)
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    get_settings.cache_clear()
    async with app.router.lifespan_context(app):
        await app.state.redis.aclose()
        app.state.redis = FakeAsyncRedis()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def scripted(*turns: list[dict]) -> ScriptedModel:
    model = ScriptedModel(*turns)
    app.dependency_overrides[get_model] = lambda: model
    return model


def turn(request: str = "dale like al post de Fede", steps: list[dict] | None = None) -> dict:
    return {"request": request, "screen": SCREEN, "steps": steps or []}


async def test_ai04_returns_the_action_the_model_chose(api):
    model = scripted([tool_use("like_post", {"ref": "p2"})])

    response = await api.post(URL, json=turn(), headers=signed_in())

    assert response.status_code == 200
    assert response.json() == {
        "action": "like_post",
        "arguments": {"ref": "p2"},
        "needsConfirmation": False,
        "finished": False,
    }
    # The first step carries the request and the screen, as the evaluation did.
    [[first]] = model.received
    assert "dale like al post de Fede" in first["content"][0]["text"]
    assert "Alguien juega al fútbol el sábado?" in first["content"][0]["text"]


async def test_ai04_rejects_an_action_outside_the_catalog(api):
    model = scripted(
        [tool_use("delete_post", {"ref": "p1"})],
        [tool_use("finish", {"say": "Borrar lo tenés que hacer vos."}, "t2")],
    )

    response = await api.post(URL, json=turn("borrá el post de Ana"), headers=signed_in())

    assert response.json()["action"] == "finish"
    assert response.json()["finished"] is True
    # The model was told why, and the refused action never reached the app.
    refused = model.received[1][-1]["content"][0]["toolResult"]
    assert refused["status"] == "error"
    assert refused["content"][0]["json"]["error"] == "delete_post no está en el catálogo"


async def test_ai04_rejects_a_reference_that_is_not_on_screen(api):
    model = scripted(
        [tool_use("like_post", {"ref": "p9"})],
        [tool_use("like_post", {"ref": "p2"}, "t2")],
    )

    response = await api.post(URL, json=turn(), headers=signed_in())

    assert response.json()["arguments"] == {"ref": "p2"}
    refused = model.received[1][-1]["content"][0]["toolResult"]
    assert refused["content"][0]["json"]["error"] == "p9 no está en la pantalla"


async def test_a_second_refusal_ends_the_request(api):
    scripted([tool_use("like_post", {"ref": "p9"})], [tool_use("like_post", {"ref": "p8"}, "t2")])

    response = await api.post(URL, json=turn(), headers=signed_in())

    assert response.json() == {
        "action": "finish",
        "arguments": {"say": NOT_DONE_SAY},
        "needsConfirmation": False,
        "finished": True,
    }


async def test_ai04_finishes_at_the_step_limit(api):
    model = scripted()
    done = {"action": "scroll_feed", "arguments": {"direction": "down"}, "result": {"ok": True}}

    response = await api.post(
        URL, json=turn("bajá hasta el final", [done] * MAX_STEPS), headers=signed_in()
    )

    assert response.json()["arguments"] == {"say": STEP_LIMIT_SAY}
    assert response.json()["finished"] is True
    assert model.received == []


async def test_the_conversation_is_rebuilt_from_the_steps(api):
    model = scripted([tool_use("finish", {"say": "Listo, le di like."})])
    liked = {"action": "like_post", "arguments": {"ref": "p2"}, "result": {"ok": True}}

    response = await api.post(URL, json=turn(steps=[liked]), headers=signed_in())

    assert response.json()["arguments"] == {"say": "Listo, le di like."}
    request, action, result = model.received[0]
    assert request["content"][0]["text"] == "Pedido: dale like al post de Fede"
    assert action == {
        "role": "assistant",
        "content": [tool_use("like_post", {"ref": "p2"}, "paso1")],
    }
    # The screen the step left travels with its result.
    answer = result["content"][0]["toolResult"]["content"][0]["json"]
    assert answer == {"ok": True, "pantalla": SCREEN}


async def test_an_action_seen_by_others_asks_for_confirmation(api):
    scripted([tool_use("retweet_post", {"ref": "p1"})])

    response = await api.post(URL, json=turn("retuiteá lo de Ana"), headers=signed_in())

    assert response.json()["needsConfirmation"] is True


async def test_an_answer_without_an_action_is_read_aloud(api):
    scripted([{"text": "No entendí qué querés hacer."}])

    response = await api.post(URL, json=turn("eh"), headers=signed_in())

    assert response.json()["arguments"] == {"say": "No entendí qué querés hacer."}


async def test_ai04_answers_401_without_a_token(api):
    scripted()

    missing = await api.post(URL, json=turn())
    forged = await api.post(URL, json=turn(), headers=signed_in(key=Ed25519PrivateKey.generate()))

    assert (missing.status_code, forged.status_code) == (401, 401)
    assert missing.json()["type"].endswith("/invalid-token")
    assert missing.headers["content-type"] == "application/problem+json"


async def test_a_backoffice_token_is_refused(api):
    scripted()

    response = await api.post(URL, json=turn(), headers=signed_in(role="moderator"))

    assert response.status_code == 403
    assert response.json()["type"].endswith("/app-account-required")


async def test_ai04_answers_429_past_the_rate_limit(api):
    app.state.settings.turns_per_hour = 2
    user, other = uuid.uuid4(), uuid.uuid4()
    scripted(*[[tool_use("refresh_feed", {})]] * 3)

    answers = [(await api.post(URL, json=turn(), headers=signed_in(user))) for _ in range(3)]
    elsewhere = await api.post(URL, json=turn(), headers=signed_in(other))

    assert [one.status_code for one in answers] == [200, 200, 429]
    assert answers[2].json()["type"].endswith("/too-many-requests")
    assert 0 < int(answers[2].headers["retry-after"]) <= 3600
    # The count is per account.
    assert elsewhere.status_code == 200


async def test_a_rate_limit_that_cannot_be_read_refuses_the_step(api):
    class DownRedis:
        def pipeline(self, transaction: bool = True):
            raise RedisConnectionError("Redis is down")

        async def aclose(self) -> None:
            pass

    app.state.redis = DownRedis()
    scripted()

    response = await api.post(URL, json=turn(), headers=signed_in())

    assert response.status_code == 503
    assert response.json()["type"].endswith("/rate-limit-unavailable")


async def test_ai04_answers_503_when_it_is_turned_off(api):
    app.state.settings.assistant_enabled = False
    model = scripted()

    # Before the token: turned off is off for everyone.
    response = await api.post(URL, json=turn())

    assert response.status_code == 503
    assert response.json()["type"].endswith("/assistant-off")
    assert model.received == []


async def test_without_a_model_configured_it_answers_503(api):
    response = await api.post(URL, json=turn(), headers=signed_in())

    assert response.status_code == 503
    assert response.json()["type"].endswith("/assistant-not-configured")


async def test_the_model_built_at_startup_answers(api):
    # What runs in the cluster: the model the service built, not an override.
    app.state.model = ScriptedModel([tool_use("refresh_feed", {})])

    response = await api.post(URL, json=turn("actualizá el feed"), headers=signed_in())

    assert response.json()["action"] == "refresh_feed"


async def test_with_an_arn_it_builds_a_bedrock_model(monkeypatch):
    monkeypatch.setenv("JWT_PUBLIC_KEY", PUBLIC_PEM)
    monkeypatch.setenv("BEDROCK_MODEL_ID", "arn:aws:bedrock:us-east-2:000000000000:profile/test")
    get_settings.cache_clear()

    async with app.router.lifespan_context(app):
        model = app.state.model

    get_settings.cache_clear()
    assert isinstance(model, BedrockModel)
    assert model.model_id.endswith("profile/test")


def test_a_key_that_is_not_ed25519_is_refused_at_startup():
    other = ec.generate_private_key(ec.SECP256R1()).public_key()
    pem = other.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    with pytest.raises(ValueError, match="Ed25519"):
        load_public_key(pem)


@pytest.mark.parametrize(
    "body",
    [
        {"request": "", "screen": SCREEN},
        {"request": "x" * 501, "screen": SCREEN},
        {"request": "hola", "screen": {"posts": ["x" * 16_001]}},
        {
            "request": "hola",
            "screen": SCREEN,
            "steps": [{"action": "refresh_feed", "arguments": {}, "result": {"x": "y" * 2001}}],
        },
    ],
)
async def test_what_the_app_sends_is_limited_in_size(api, body):
    model = scripted()

    response = await api.post(URL, json=body, headers=signed_in())

    assert response.status_code == 422
    assert response.json()["type"].endswith("/validation-failed")
    assert model.received == []
