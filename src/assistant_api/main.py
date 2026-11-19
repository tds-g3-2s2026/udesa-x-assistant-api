import logging
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from redis.asyncio import Redis

from assistant_api.agent.bedrock import BedrockModel
from assistant_api.guards import (
    ProblemError,
    load_public_key,
    problem_error_handler,
    validation_error_handler,
)
from assistant_api.settings import get_settings
from assistant_api.telemetry import export, instrument, meter_provider
from assistant_api.turns import router as turns_router

API_PREFIX = "/api"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Uvicorn configures only its own loggers and leaves the root at WARNING,
    # so without this every logger.info in the service is dropped.
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        force=True,
    )
    logger_provider = None
    metric_reader = None
    if settings.otel_exporter_otlp_endpoint:
        logger_provider = export(OTLPSpanExporter(), OTLPLogExporter())
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        meter_provider.add_metric_reader(metric_reader)

    app.state.settings = settings
    app.state.jwt_public_key = load_public_key(settings.jwt_public_key)
    app.state.redis = Redis.from_url(settings.redis_url)
    # No model without its ARN: the endpoint answers 503 instead of failing on every call.
    app.state.model = (
        BedrockModel(settings.bedrock_model_id, region=settings.bedrock_region)
        if settings.bedrock_model_id
        else None
    )
    try:
        yield
    finally:
        try:
            await app.state.redis.aclose()
        finally:
            # What is still pending leaves before the pod stops.
            if metric_reader is not None:
                try:
                    metric_reader.force_flush()
                finally:
                    meter_provider.remove_metric_reader(metric_reader)
            if logger_provider is not None:
                logger_provider.shutdown()


app = FastAPI(title="UdeSA-X Assistant API", version=version("assistant-api"), lifespan=lifespan)
instrument(app)
app.add_exception_handler(ProblemError, problem_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
# Under /api like every other service: the gateway forwards the path as it comes.
app.include_router(turns_router, prefix=API_PREFIX)


@app.get("/healthcheck", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Up as soon as the process is. Redis and Bedrock are only reached by a request."""
    return {"status": "ok"}
