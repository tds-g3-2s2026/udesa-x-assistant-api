"""The metrics and traces of a step, against a scripted model.

Grafana gets numbers and catalog names, never text (ADR-023). Langfuse gets the
agent's spans with what the model read and answered (ADR-027).
"""

import json
import logging

import pytest
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from assistant_api import main, telemetry
from assistant_api.agent.loop import MAX_STEPS
from assistant_api.agent.turn import PastStep, next_step
from assistant_api.settings import get_settings
from assistant_api.turns import get_model
from tests.integration.test_turns import PUBLIC_PEM, SCREEN, ScriptedModel, signed_in, tool_use

REQUEST = "dale like al post de Fede"
CALLER_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_TRACEPARENT = f"00-{CALLER_TRACE_ID}-00f067aa0ba902b7-01"


@pytest.fixture
def recorded():
    """What the metrics hold, from the moment the test starts."""
    reader = InMemoryMetricReader()
    telemetry.meter_provider.add_metric_reader(reader)

    def read() -> dict[str, list]:
        data = reader.get_metrics_data()
        return {
            metric.name: list(metric.data.data_points)
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }

    yield read
    telemetry.meter_provider.remove_metric_reader(reader)


@pytest.fixture(scope="module")
def spans():
    exporter = InMemorySpanExporter()
    telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.shutdown()


def every_value(points: dict[str, list]) -> list[str]:
    return [
        str(value) for items in points.values() for p in items for value in p.attributes.values()
    ]


def test_ai04_a_step_is_measured_by_its_action_and_outcome(recorded):
    model = ScriptedModel([tool_use("like_post", {"ref": "p2"})])

    next_step(model, REQUEST, SCREEN, [])

    points = recorded()
    [step] = points["assistant.steps"]
    assert (dict(step.attributes), step.value) == ({"action": "like_post", "outcome": "action"}, 1)
    tokens = {p.attributes["type"]: p.value for p in points["assistant.model.tokens"]}
    assert tokens == {"input": 900, "output": 30}
    assert points["assistant.model.duration"][0].count == 1
    # Not a word of the request, the posts or the answer reaches a metric.
    assert not any(word in value for value in every_value(points) for word in ("Fede", "p2"))


def test_ai04_the_spoken_answer_is_measured_by_its_length(recorded):
    model = ScriptedModel([tool_use("finish", {"say": "Listo, le di like."})])

    next_step(model, REQUEST, SCREEN, [])

    points = recorded()
    assert dict(points["assistant.steps"][0].attributes) == {
        "action": "finish",
        "outcome": "finished",
    }
    [length] = points["assistant.spoken.length"]
    assert (length.count, length.sum) == (1, len("Listo, le di like."))


def test_a_request_out_of_steps_is_counted_without_calling_the_model(recorded):
    steps = [PastStep("scroll_feed", {"direction": "down"}, {"ok": True})] * MAX_STEPS

    next_step(ScriptedModel(), REQUEST, SCREEN, steps)

    points = recorded()
    assert dict(points["assistant.steps"][0].attributes)["outcome"] == "step_limit"
    assert "assistant.model.tokens" not in points


@pytest.fixture(scope="module")
def grafana():
    """What the exporter to Grafana Cloud gets."""
    exporter = InMemorySpanExporter()
    processor = telemetry.WithoutContent(SimpleSpanProcessor(exporter))
    telemetry.tracer_provider.add_span_processor(processor)
    yield exporter
    exporter.shutdown()


@pytest.fixture(scope="module")
def langfuse():
    """What the exporter to the Langfuse project gets."""
    exporter = InMemorySpanExporter()
    processor = telemetry.OnlyAgentSpans(SimpleSpanProcessor(exporter))
    telemetry.tracer_provider.add_span_processor(processor)
    yield exporter
    exporter.shutdown()


def named(exporter: InMemorySpanExporter, name: str) -> list:
    return [span for span in exporter.get_finished_spans() if span.name == name]


def test_ai04_each_call_to_the_model_is_a_generation_with_its_tokens_and_cost(spans):
    spans.clear()
    # The first action is refused: the model is called twice in the same step.
    model = ScriptedModel(
        [tool_use("like_post", {"ref": "p9"})], [tool_use("like_post", {"ref": "p2"})]
    )

    next_step(model, REQUEST, SCREEN, [])

    calls = named(spans, telemetry.MODEL_SPAN)
    assert len(calls) == 2
    attributes = calls[0].attributes
    assert attributes["langfuse.observation.type"] == "generation"
    assert attributes["gen_ai.request.model"] == "scripted"
    tokens = (attributes["gen_ai.usage.input_tokens"], attributes["gen_ai.usage.output_tokens"])
    assert tokens == (900, 30)
    assert attributes["gen_ai.usage.cost"] == pytest.approx(0.00105)
    # What the model read, system prompt first, and what it answered.
    read = json.loads(attributes["langfuse.observation.input"])
    assert read[0]["role"] == "system"
    assert REQUEST in read[1]["content"][0]["text"]
    assert json.loads(attributes["langfuse.observation.output"]) == [
        tool_use("like_post", {"ref": "p9"})
    ]


def test_the_step_is_a_span_with_what_the_app_sent_and_got(spans):
    spans.clear()
    model = ScriptedModel([tool_use("like_post", {"ref": "p2"})])

    next_step(model, REQUEST, SCREEN, [])

    [step] = named(spans, telemetry.STEP_SPAN)
    attributes = step.attributes
    assert attributes["langfuse.observation.type"] == "agent"
    assert attributes["langfuse.observation.metadata.step"] == 1
    assert attributes["langfuse.observation.metadata.action"] == "like_post"
    assert attributes["langfuse.observation.metadata.outcome"] == "action"
    assert attributes["langfuse.observation.level"] == "DEFAULT"
    sent = json.loads(attributes["langfuse.observation.input"])
    assert sent == {"pedido": REQUEST, "pantalla": SCREEN, "pasos": []}
    assert json.loads(attributes["langfuse.observation.output"]) == {
        "action": "like_post",
        "arguments": {"ref": "p2"},
    }
    [call] = named(spans, telemetry.MODEL_SPAN)
    assert call.parent.span_id == step.context.span_id


def test_a_step_the_server_ended_is_a_warning(spans):
    spans.clear()
    steps = [PastStep("scroll_feed", {"direction": "down"}, {"ok": True})] * MAX_STEPS

    next_step(ScriptedModel(), REQUEST, SCREEN, steps)

    [step] = named(spans, telemetry.STEP_SPAN)
    assert step.attributes["langfuse.observation.metadata.outcome"] == "step_limit"
    assert step.attributes["langfuse.observation.level"] == "WARNING"


def test_grafana_gets_the_agent_spans_without_a_word_of_what_was_said(grafana):
    grafana.clear()
    model = ScriptedModel([tool_use("like_post", {"ref": "p2"})])

    next_step(model, REQUEST, SCREEN, [])

    finished = grafana.get_finished_spans()
    assert {span.name for span in finished} == {telemetry.STEP_SPAN, telemetry.MODEL_SPAN}
    values = [str(value) for span in finished for value in span.attributes.values()]
    # Not the request, a post of the screen or the model's answer (ADR-023).
    for said in (REQUEST, "fútbol", '"ref": "p2"'):
        assert not any(said in value for value in values)
    [call] = named(grafana, telemetry.MODEL_SPAN)
    assert call.attributes["gen_ai.usage.input_tokens"] == 900
    assert call.attributes["gen_ai.usage.cost"] == pytest.approx(0.00105)


@pytest.fixture
async def api(monkeypatch):
    monkeypatch.setenv("JWT_PUBLIC_KEY", PUBLIC_PEM)
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    get_settings.cache_clear()
    async with main.app.router.lifespan_context(main.app):
        await main.app.state.redis.aclose()
        main.app.state.redis = FakeAsyncRedis()
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    main.app.dependency_overrides.clear()
    get_settings.cache_clear()


async def test_the_model_span_joins_the_trace_of_the_request(api, spans):
    spans.clear()
    model = ScriptedModel([tool_use("like_post", {"ref": "p2"})])
    main.app.dependency_overrides[get_model] = lambda: model

    response = await api.post(
        "/api/assistant/turns",
        json={"request": REQUEST, "screen": SCREEN},
        headers={**signed_in(), "traceparent": CALLER_TRACEPARENT},
    )

    assert response.status_code == 200
    [call] = named(spans, telemetry.MODEL_SPAN)
    assert format(call.context.trace_id, "032x") == CALLER_TRACE_ID
    assert call.parent is not None


async def test_langfuse_gets_the_step_as_the_root_of_the_request_trace(api, langfuse):
    langfuse.clear()
    model = ScriptedModel([tool_use("like_post", {"ref": "p2"})])
    main.app.dependency_overrides[get_model] = lambda: model

    await api.post(
        "/api/assistant/turns",
        json={"request": REQUEST, "screen": SCREEN},
        headers={**signed_in(), "traceparent": CALLER_TRACEPARENT},
    )

    # Neither the HTTP span nor anything else that is not the agent's.
    finished = langfuse.get_finished_spans()
    assert sorted(span.name for span in finished) == [telemetry.MODEL_SPAN, telemetry.STEP_SPAN]
    [step] = named(langfuse, telemetry.STEP_SPAN)
    assert step.parent is None
    # The trace id Grafana shows for the same request: one leads to the other.
    assert format(step.context.trace_id, "032x") == CALLER_TRACE_ID
    assert REQUEST in step.attributes["langfuse.observation.input"]


def test_langfuse_is_one_more_destination_for_the_agent_spans(monkeypatch):
    added: list = []
    monkeypatch.setattr(telemetry.tracer_provider, "add_span_processor", added.append)

    telemetry.export_to_langfuse(InMemorySpanExporter())

    [processor] = added
    assert isinstance(processor, telemetry.OnlyAgentSpans)


def test_the_filters_pass_flush_and_shutdown_on_to_the_exporter():
    exporter = InMemorySpanExporter()
    for processor in (
        telemetry.WithoutContent(SimpleSpanProcessor(exporter)),
        telemetry.OnlyAgentSpans(SimpleSpanProcessor(exporter)),
    ):
        assert processor.force_flush() is True
        processor.shutdown()


async def test_with_an_endpoint_the_service_exports_and_flushes_on_shutdown(monkeypatch):
    readers: list[InMemoryMetricReader] = []
    removed: list[InMemoryMetricReader] = []
    logs = InMemoryLogRecordExporter()
    remove = telemetry.meter_provider.remove_metric_reader

    def reader(_exporter) -> InMemoryMetricReader:
        readers.append(InMemoryMetricReader())
        return readers[-1]

    monkeypatch.setenv("JWT_PUBLIC_KEY", PUBLIC_PEM)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.test")
    monkeypatch.setattr(main, "OTLPSpanExporter", InMemorySpanExporter)
    monkeypatch.setattr(main, "OTLPLogExporter", lambda: logs)
    monkeypatch.setattr(main, "OTLPMetricExporter", lambda: None)
    monkeypatch.setattr(main, "PeriodicExportingMetricReader", reader)
    monkeypatch.setattr(
        telemetry.meter_provider,
        "remove_metric_reader",
        lambda done: (removed.append(done), remove(done)),
    )
    get_settings.cache_clear()

    async with main.app.router.lifespan_context(main.app):
        logging.getLogger("assistant_api.test").warning("inside the service")
        handlers = [h for h in logging.getLogger().handlers if isinstance(h, LoggingHandler)]

    get_settings.cache_clear()
    for handler in handlers:
        logging.getLogger().removeHandler(handler)
    # The reader is gone once the service stops, so the next start does not export twice.
    assert len(readers) == 1
    assert removed == readers
    # The log provider shut down, so what was logged already left.
    assert "inside the service" in [record.log_record.body for record in logs.get_finished_logs()]
