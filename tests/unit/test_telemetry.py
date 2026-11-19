"""The metrics and traces of a step, against a scripted model: numbers, never text."""

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


def test_ai04_each_call_to_the_model_is_a_span_with_its_tokens(spans):
    spans.clear()
    # The first action is refused: the model is called twice in the same step.
    model = ScriptedModel(
        [tool_use("like_post", {"ref": "p9"})], [tool_use("like_post", {"ref": "p2"})]
    )

    next_step(model, REQUEST, SCREEN, [])

    calls = [span for span in spans.get_finished_spans() if span.name == "assistant.model"]
    assert len(calls) == 2
    assert dict(calls[0].attributes) == {
        "gen_ai.usage.input_tokens": 900,
        "gen_ai.usage.output_tokens": 30,
    }


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
    [call] = [span for span in spans.get_finished_spans() if span.name == "assistant.model"]
    assert format(call.context.trace_id, "032x") == CALLER_TRACE_ID
    assert call.parent is not None


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
