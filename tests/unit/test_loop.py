"""The agent loop against a scripted model: no call ever reaches Bedrock."""

from assistant_api.agent.loop import MAX_STEPS, ONE_ACTION_PER_STEP, ModelTurn, run_request


def tool_use(name: str, arguments: dict, tool_use_id: str = "t1") -> dict:
    return {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": arguments}}


class ScriptedModel:
    """Answers each turn with the next scripted content, and keeps what it was sent."""

    def __init__(self, *turns: list[dict], then: list[dict] | None = None) -> None:
        self.turns = list(turns)
        self.then = then
        self.received: list[list[dict]] = []

    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.received.append(list(messages))
        content = self.turns.pop(0) if self.turns else self.then
        return ModelTurn(content=content, input_tokens=100, output_tokens=10, latency_ms=50.0)


class RecordingApp:
    def __init__(self) -> None:
        self.performed: list[tuple[str, dict]] = []

    def describe(self) -> dict:
        return {"screen": "feed", "posts": [{"ref": "p1", "text": "Hola"}]}

    def perform(self, name: str, arguments: dict) -> dict:
        self.performed.append((name, arguments))
        return {"ok": True, "done": "hecho"}


def last_answers(model: ScriptedModel) -> list[dict]:
    """The tool results the loop sent back on the model's last turn."""
    return [block["toolResult"] for block in model.received[-1][-1]["content"]]


def test_ai03_stops_at_the_step_limit():
    model = ScriptedModel(then=[tool_use("scroll_feed", {"direction": "down"})])
    app = RecordingApp()

    transcript = run_request(model, app, "bajá hasta el final")

    assert transcript.outcome == "step_limit"
    assert len(model.received) == MAX_STEPS
    assert len(app.performed) == MAX_STEPS
    assert transcript.said is None


def test_ai03_an_action_outside_the_catalog_is_not_executed():
    model = ScriptedModel(
        [tool_use("delete_post", {"ref": "p1"})],
        [tool_use("finish", {"say": "Eso lo tenés que hacer vos"}, "t2")],
    )
    app = RecordingApp()

    transcript = run_request(model, app, "borrá mi último post")

    assert app.performed == []
    assert transcript.steps[0].result == {
        "ok": False,
        "error": "delete_post no está en el catálogo",
    }
    # The model is told it failed, and finishes on its next turn.
    assert last_answers(model)[0]["status"] == "error"
    assert transcript.outcome == "finished"


def test_ai03_finish_ends_the_request():
    model = ScriptedModel(
        [tool_use("like_post", {"ref": "p1"})],
        [tool_use("finish", {"say": "Listo, le di like"}, "t2")],
        [tool_use("like_post", {"ref": "p1"}, "t3")],
    )
    app = RecordingApp()

    transcript = run_request(model, app, "dale like al primero")

    assert transcript.outcome == "finished"
    assert transcript.said == "Listo, le di like"
    assert app.performed == [("like_post", {"ref": "p1"})]
    assert len(model.received) == 2


def test_the_first_message_carries_the_request_and_the_screen():
    model = ScriptedModel([tool_use("finish", {"say": "Listo"})])

    run_request(model, RecordingApp(), "poné el modo oscuro")

    first = model.received[0][0]["content"][0]["text"]
    assert first.startswith("Pedido: poné el modo oscuro")
    assert '"ref": "p1"' in first


def test_the_app_answers_with_the_new_screen():
    model = ScriptedModel(
        [tool_use("like_post", {"ref": "p1"})], [tool_use("finish", {"say": "Listo"}, "t2")]
    )

    run_request(model, RecordingApp(), "dale like al primero")

    answer = last_answers(model)[0]
    assert "status" not in answer
    assert answer["content"][0]["json"]["pantalla"]["screen"] == "feed"


def test_a_text_that_breaks_the_rules_never_reaches_the_app():
    model = ScriptedModel(
        [tool_use("compose_post", {"text": "x" * 281})],
        [tool_use("finish", {"say": "No pude"}, "t2")],
    )
    app = RecordingApp()

    transcript = run_request(model, app, "publicá algo largo")

    assert app.performed == []
    assert transcript.steps[0].result["error"] == "El texto supera los 280 caracteres"


def test_only_the_first_of_several_actions_runs():
    model = ScriptedModel(
        [tool_use("like_post", {"ref": "p1"}, "t1"), tool_use("retweet_post", {"ref": "p1"}, "t2")],
        [tool_use("finish", {"say": "Listo"}, "t3")],
    )
    app = RecordingApp()

    run_request(model, app, "dale like y retuiteá")

    assert app.performed == [("like_post", {"ref": "p1"})]
    answers = last_answers(model)
    # Converse wants an answer for every tool the model called.
    assert [answer["toolUseId"] for answer in answers] == ["t1", "t2"]
    assert answers[1]["content"][0]["json"]["error"] == ONE_ACTION_PER_STEP


def test_a_finish_without_words_is_refused():
    model = ScriptedModel(
        [tool_use("finish", {"say": "  "})], [tool_use("finish", {"say": "Listo"}, "t2")]
    )

    transcript = run_request(model, RecordingApp(), "nada")

    assert transcript.steps[0].result == {"ok": False, "error": "Falta say"}
    assert transcript.said == "Listo"


def test_an_answer_without_a_tool_ends_the_request():
    model = ScriptedModel([{"text": "No sé qué hacer"}])

    transcript = run_request(model, RecordingApp(), "hacé algo")

    assert transcript.outcome == "no_action"
    assert transcript.said == "No sé qué hacer"
    assert transcript.steps == []


def test_tokens_add_up_across_turns():
    model = ScriptedModel(
        [tool_use("like_post", {"ref": "p1"})], [tool_use("finish", {"say": "Listo"}, "t2")]
    )

    transcript = run_request(model, RecordingApp(), "dale like")

    assert (transcript.input_tokens, transcript.output_tokens) == (200, 20)
