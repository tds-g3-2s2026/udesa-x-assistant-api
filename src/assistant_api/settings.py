"""What the service reads from the environment. In the cluster, the ConfigMap and the secrets."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # The ARN of the group's inference profile. Never written in the repo: it
    # arrives as a secret. Empty, the endpoint answers 503 instead of failing
    # on every call.
    bedrock_model_id: str = ""
    bedrock_region: str = "us-east-2"

    # The public half of the key users-api signs its access tokens with.
    jwt_public_key: str
    jwt_issuer: str = "users-api"

    # `/2`: users-api uses `/0` and posts-api `/1` (ADR-009).
    redis_url: str = "redis://localhost:6379/2"

    # Off turns the endpoint into a 503 without a deploy of the code: the way
    # to stop spending if something goes wrong.
    assistant_enabled: bool = True
    # Twenty requests of up to eight steps an hour. Each step costs under a
    # thousandth of a dollar: the limit is there for a client stuck in a loop.
    turns_per_hour: int = 160


@lru_cache
def get_settings() -> Settings:
    return Settings()
