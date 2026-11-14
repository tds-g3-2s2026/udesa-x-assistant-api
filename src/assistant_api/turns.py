"""`POST /assistant/turns`: the next action of a spoken request, one call per step.

The app calls it once per step: with the request and the screen to start, and
then with the steps it already took and the screen they left. It answers one
action of the catalog for the app to perform, or `finish` with the sentence the
app reads aloud.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from assistant_api.agent.loop import MAX_STEPS, Model
from assistant_api.agent.turn import PastStep, next_step
from assistant_api.guards import AllowedUserDep, ProblemError

# What the app sends is limited in size as well as in shape: everything here
# goes to the model on every step, and the model is paid by the token.
MAX_REQUEST_LENGTH = 500
MAX_SCREEN_LENGTH = 16_000
MAX_RESULT_LENGTH = 2_000

router = APIRouter(prefix="/assistant", tags=["assistant"])


def json_length(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False))


class StepIn(BaseModel):
    action: str = Field(max_length=40)
    arguments: dict[str, str]
    # What the app answered when it performed the action.
    result: dict


class TurnRequest(BaseModel):
    request: str = Field(min_length=1, max_length=MAX_REQUEST_LENGTH)
    screen: dict
    # Up to the limit: a request that already took all its steps gets `finish`.
    steps: list[StepIn] = Field(default_factory=list, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def within_size(self) -> "TurnRequest":
        if json_length(self.screen) > MAX_SCREEN_LENGTH:
            raise ValueError(f"La pantalla supera los {MAX_SCREEN_LENGTH} caracteres")
        for step in self.steps:
            if json_length(step.result) > MAX_RESULT_LENGTH:
                raise ValueError(f"Un resultado supera los {MAX_RESULT_LENGTH} caracteres")
        return self


class TurnResponse(BaseModel):
    action: str
    arguments: dict[str, str]
    # Retweeting, replying, posting and following are shown to the user first.
    needs_confirmation: bool = Field(serialization_alias="needsConfirmation")
    # True on `finish`: the request ends and the app reads `arguments.say` aloud.
    finished: bool


def get_model(request: Request) -> Model:
    model = request.app.state.model
    if model is None:
        raise ProblemError(
            status=503,
            code="assistant-not-configured",
            title="El asistente no está disponible",
            detail="El asistente por voz no está configurado",
        )
    return model


ModelDep = Annotated[Model, Depends(get_model)]


@router.post("/turns")
async def take_turn(body: TurnRequest, _: AllowedUserDep, model: ModelDep) -> TurnResponse:
    steps = [PastStep(step.action, step.arguments, step.result) for step in body.steps]
    # boto3 does not do asyncio: the call to Bedrock waits on a thread, not on the loop.
    decision = await run_in_threadpool(next_step, model, body.request, body.screen, steps)
    return TurnResponse(
        action=decision.action,
        arguments=decision.arguments,
        needs_confirmation=decision.needs_confirmation,
        finished=decision.action == "finish",
    )
