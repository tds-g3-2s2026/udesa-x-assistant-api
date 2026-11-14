"""What keeps a request out, and how the app is told: the switch, the token and the rate limit.

Refusals travel as Problem Details, the same format users-api and posts-api
answer with, so the app reads them the same way.
"""

import time
import uuid
from typing import Annotated

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError

TOKEN_ALGORITHM = "EdDSA"
PROBLEM_MEDIA_TYPE = "application/problem+json"
_TYPE_BASE = "https://udesa-x.dev/errors"
HOUR = 3600


class ProblemError(Exception):
    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.headers = headers or {}


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    errors: list[dict] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {
        "type": f"{_TYPE_BASE}/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=headers)


async def problem_error_handler(request: Request, exc: ProblemError) -> JSONResponse:
    return problem_response(
        request,
        status=exc.status,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"] if part != "body"),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return problem_response(
        request,
        status=422,
        code="validation-failed",
        title="No se pudo procesar el pedido",
        detail="Algún dato del pedido no es válido",
        errors=errors,
    )


def assistant_on(request: Request) -> None:
    """503 while the assistant is turned off, before anything else is checked."""
    if not request.app.state.settings.assistant_enabled:
        raise ProblemError(
            status=503,
            code="assistant-off",
            title="El asistente no está disponible",
            detail="El asistente por voz está apagado. Probá más tarde",
        )


def load_public_key(pem: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("JWT_PUBLIC_KEY must be an Ed25519 public key in PEM format")
    return key


# auto_error off: a missing header answers 401 like any other bad token, not
# the 403 FastAPI would send.
bearer_scheme = HTTPBearer(auto_error=False)


def unauthorized() -> ProblemError:
    return ProblemError(
        status=401,
        code="invalid-token",
        title="No se pudo autenticar la solicitud",
        detail="El token no es válido",
    )


def current_user(
    request: Request,
    _: Annotated[None, Depends(assistant_on)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> uuid.UUID:
    """The app account behind the access token, checked like posts-api does.

    Signature, expiry and issuer only. The revocation marks are not read: the
    assistant only suggests actions, and every action the app then performs
    goes through posts-api, which does read them. A backoffice token is refused:
    the assistant is for app accounts.
    """
    if credentials is None:
        raise unauthorized()
    try:
        claims = jwt.decode(
            credentials.credentials,
            request.app.state.jwt_public_key,
            algorithms=[TOKEN_ALGORITHM],
            issuer=request.app.state.settings.jwt_issuer,
            options={"require": ["iss", "sub", "exp"]},
        )
        user_id = uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise unauthorized() from exc
    if claims.get("role", "user") != "user":
        raise ProblemError(
            status=403,
            code="app-account-required",
            title="No se pudo usar el asistente",
            detail="El asistente es para las cuentas de la app",
        )
    return user_id


async def within_rate_limit(
    request: Request, user_id: Annotated[uuid.UUID, Depends(current_user)]
) -> uuid.UUID:
    """Count the step against the user's hour, and refuse it past the limit.

    A fixed window per clock hour: one key per user and hour, which expires on
    its own. If Redis does not answer the step is refused: letting it through
    would leave the monthly quota of the course without a ceiling.
    """
    window = int(time.time()) // HOUR
    key = f"assistant:turns:{user_id}:{window}"
    try:
        async with request.app.state.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, HOUR, nx=True)
            count, _ = await pipe.execute()
    except RedisError as exc:
        raise ProblemError(
            status=503,
            code="rate-limit-unavailable",
            title="El asistente no está disponible",
            detail="No pudimos verificar tu límite de uso. Probá de nuevo en unos minutos",
        ) from exc
    if count > request.app.state.settings.turns_per_hour:
        retry_after = HOUR - int(time.time()) % HOUR
        raise ProblemError(
            status=429,
            code="too-many-requests",
            title="Llegaste al límite del asistente",
            detail="Usaste el asistente muchas veces en la última hora. Probá más tarde",
            headers={"Retry-After": str(retry_after)},
        )
    return user_id


AllowedUserDep = Annotated[uuid.UUID, Depends(within_rate_limit)]
