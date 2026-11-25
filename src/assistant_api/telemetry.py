"""Traces, logs and metrics of the service, and what the model read and answered.

The same shape as posts-api (ADR-013): a span per request, each log line with
its trace, HTTP durations in seconds. On top, the assistant's own metrics. None
of what reaches Grafana carries the text of a request, a post or an answer:
only names from the catalog, outcomes and numbers (ADR-023).

The agent's spans, a step and each call to the model, also go to Langfuse,
and only there with the text: the project of the team and the tutor, where
each call is read with its prompt, answer, tokens and cost (ADR-027).

The providers live here, and not in `main`, so the agent records its steps
without importing the app.
"""

import logging
import os
import time
from importlib.metadata import version
from uuid import uuid4

from fastapi import FastAPI, Request
from opentelemetry.context import Context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.view import (
    DropAggregation,
    ExplicitBucketHistogramAggregation,
    View,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

SERVICE_NAME = "assistant-api"
SERVICE_INSTANCE_ID = str(uuid4())
HTTP_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 7.5, 10, 15)
# Seconds. A call to the model takes about one; ten is where the client gives up.
MODEL_DURATION_BUCKETS = (0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 7.5, 10)
# Characters. The prompt asks for a sentence; the spike measured up to 252.
SPOKEN_LENGTH_BUCKETS = (20, 40, 60, 80, 100, 150, 200, 300)

# Kubernetes probes hit these every few seconds. A span and a log line per
# probe would bury the requests somebody actually made.
PROBE_PATHS = ("/healthcheck",)

request_logger = logging.getLogger("assistant_api.requests")


def _resource() -> Resource:
    return Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": version(SERVICE_NAME),
            "service.instance.id": SERVICE_INSTANCE_ID,
        }
    )


def _histogram(name: str, buckets: tuple[float, ...]) -> View:
    return View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(buckets))


meter_provider = MeterProvider(
    resource=_resource(),
    views=[
        View(
            instrument_name="http.server.request.duration",
            attribute_keys={"http.request.method", "http.route", "http.response.status_code"},
            aggregation=ExplicitBucketHistogramAggregation(HTTP_DURATION_BUCKETS),
        ),
        _histogram("assistant.model.duration", MODEL_DURATION_BUCKETS),
        _histogram("assistant.spoken.length", SPOKEN_LENGTH_BUCKETS),
        View(instrument_name="assistant.steps"),
        View(instrument_name="assistant.model.tokens"),
        View(instrument_name="*", aggregation=DropAggregation()),
    ],
)
tracer_provider = TracerProvider(resource=_resource())

# The agent's spans: one per step and one per call to the model inside it.
AGENT_SCOPE = "assistant_api.agent"
STEP_SPAN = "assistant.step"
MODEL_SPAN = "assistant.model"
agent_tracer = tracer_provider.get_tracer(AGENT_SCOPE)
# What the model read and answered. Langfuse gets these and nobody else.
CONTENT_ATTRIBUTES = frozenset({"langfuse.observation.input", "langfuse.observation.output"})

_meter = meter_provider.get_meter("assistant_api")
steps = _meter.create_counter(
    "assistant.steps",
    unit="{step}",
    description="Steps answered, by the action the app gets and how the step ended",
)
model_duration = _meter.create_histogram(
    "assistant.model.duration", unit="s", description="How long each call to the model took"
)
model_tokens = _meter.create_counter(
    "assistant.model.tokens", unit="{token}", description="Tokens the model read and wrote"
)
spoken_length = _meter.create_histogram(
    "assistant.spoken.length",
    unit="{character}",
    description="Length of the sentence the app reads aloud at the end of a request",
)


def instrument(app: FastAPI) -> None:
    """Open a span per request, joining the caller's trace, and log each request.

    Must run before the first request, when the middleware stack is built.
    Until `export` adds an exporter the spans stay in the process, which is
    enough for the logs to carry their trace id.
    """

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        if request.url.path in PROBE_PATHS:
            return await call_next(request)
        started = time.perf_counter()
        response = await call_next(request)
        request_logger.info(
            "%s %s %s",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "http.request.method": request.method,
                "url.path": request.url.path,
                "http.response.status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response

    # Stable HTTP conventions emit durations in seconds instead of legacy milliseconds.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        excluded_urls=",".join(f"^[^:]+://[^/]+{path}$" for path in PROBE_PATHS),
        # One span per ASGI message says nothing the request span does not.
        exclude_spans=["receive", "send"],
    )


def export(span_exporter: SpanExporter, log_exporter: LogRecordExporter) -> LoggerProvider:
    """Send the spans and every log record of the process, each log tagged with its trace.

    The log handler goes on the root logger, so this has to run after any
    `logging.basicConfig(force=True)`, which removes the root handlers.
    """
    tracer_provider.add_span_processor(WithoutContent(BatchSpanProcessor(span_exporter)))
    provider = LoggerProvider(resource=_resource())
    provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    handler = LoggingHandler(logger_provider=provider)
    # The SDK logs its own export failures. Sending those through the exporter
    # that just failed retries forever, and a process with Grafana unreachable
    # never exits. They still reach the pod's output.
    handler.addFilter(lambda record: not record.name.startswith("opentelemetry"))
    logging.getLogger().addHandler(handler)
    return provider


def export_to_langfuse(span_exporter: SpanExporter) -> None:
    """Send the agent's spans, text included, to Langfuse as well (ADR-027)."""
    tracer_provider.add_span_processor(OnlyAgentSpans(BatchSpanProcessor(span_exporter)))


def copy_span(span: ReadableSpan, **changes) -> ReadableSpan:
    fields = {
        "name": span.name,
        "context": span.context,
        "parent": span.parent,
        "resource": span.resource,
        "attributes": span.attributes,
        "events": span.events,
        "links": span.links,
        "kind": span.kind,
        "status": span.status,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "instrumentation_scope": span.instrumentation_scope,
    }
    return ReadableSpan(**{**fields, **changes})


class Forwarding(SpanProcessor):
    """Hands the finished spans it lets through to the processor that exports them."""

    def __init__(self, next_processor: SpanProcessor) -> None:
        self.next_processor = next_processor

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self.next_processor.on_start(span, parent_context=parent_context)

    def shutdown(self) -> None:
        self.next_processor.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.next_processor.force_flush(timeout_millis)


class WithoutContent(Forwarding):
    """Every span, without the attributes that carry what the model read and answered."""

    def on_end(self, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        if CONTENT_ATTRIBUTES.isdisjoint(attributes):
            self.next_processor.on_end(span)
            return
        kept = {key: value for key, value in attributes.items() if key not in CONTENT_ATTRIBUTES}
        self.next_processor.on_end(copy_span(span, attributes=kept))


class OnlyAgentSpans(Forwarding):
    """The steps and their calls to the model: Langfuse never sees the HTTP spans."""

    def on_end(self, span: ReadableSpan) -> None:
        scope = span.instrumentation_scope
        if scope is None or scope.name != AGENT_SCOPE:
            return
        if span.name == STEP_SPAN:
            # Its parent is the HTTP span, which never gets there: without one,
            # the step is the root of its trace, and the trace id is still the
            # one Grafana shows for the same request.
            self.next_processor.on_end(copy_span(span, parent=None))
            return
        self.next_processor.on_end(span)
