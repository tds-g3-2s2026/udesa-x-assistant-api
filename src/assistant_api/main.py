from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from redis.asyncio import Redis

from assistant_api.agent.bedrock import BedrockModel
from assistant_api.guards import (
    ProblemError,
    load_public_key,
    problem_error_handler,
    validation_error_handler,
)
from assistant_api.settings import get_settings
from assistant_api.turns import router as turns_router

API_PREFIX = "/api"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
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
        await app.state.redis.aclose()


app = FastAPI(title="UdeSA-X Assistant API", version=version("assistant-api"), lifespan=lifespan)
app.add_exception_handler(ProblemError, problem_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
# Under /api like every other service: the gateway forwards the path as it comes.
app.include_router(turns_router, prefix=API_PREFIX)


@app.get("/healthcheck", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Up as soon as the process is. Redis and Bedrock are only reached by a request."""
    return {"status": "ok"}
