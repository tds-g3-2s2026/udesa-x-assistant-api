"""The model behind the loop: any Bedrock model that does tool use through Converse.

Which model is configuration (`BEDROCK_MODEL_ID`, the ARN of the group's
inference profile). Credentials are never here: locally they come from
`aws login --profile tds-group-3`, and in the cluster from the pod's role.
"""

import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from assistant_api.agent.loop import ModelTurn, ModelUnavailableError

# An action is a few dozen tokens and the spoken answer a sentence: this is
# room to spare, and a ceiling on what a runaway answer can cost.
MAX_TOKENS = 400

# Seconds. The first action takes about one in the spike; a call still waiting
# after ten is one the user stopped waiting for, and it frees its thread.
CONNECT_TIMEOUT = 3
READ_TIMEOUT = 10


class BedrockModel:
    def __init__(
        self,
        model_id: str,
        *,
        region: str,
        thinking: bool = False,
        client: object = None,
        name: str = "bedrock",
        prices: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self.model_id = model_id
        # The ARN names the group's account: the traces get this name instead.
        self.name = name
        self.prices = prices
        # Off by default: thinking delays the first action by seconds, and picking
        # one of a dozen actions does not need it.
        self.thinking = thinking
        # No retries: a throttled call fails at once instead of turning into
        # several calls against the group's monthly quota.
        self.client = client or boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                connect_timeout=CONNECT_TIMEOUT,
                read_timeout=READ_TIMEOUT,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )

    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        request = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": messages,
            # `any`: the model has to answer with a tool, even if it is finish.
            "toolConfig": {"tools": tools, "toolChoice": {"any": {}}},
            "inferenceConfig": {"maxTokens": MAX_TOKENS},
        }
        if not self.thinking:
            request["additionalModelRequestFields"] = {"thinking": {"type": "disabled"}}
        started = time.perf_counter()
        try:
            response = self.client.converse(**request)
        except ClientError as error:
            # Throttled, denied or rejected: the code says which, and it is safe to log.
            raise ModelUnavailableError(error.response["Error"]["Code"]) from error
        except BotoCoreError as error:
            # Timeouts and connection errors, which come without a code.
            raise ModelUnavailableError(type(error).__name__) from error
        latency_ms = (time.perf_counter() - started) * 1000
        usage = response["usage"]
        return ModelTurn(
            content=response["output"]["message"]["content"],
            input_tokens=usage["inputTokens"],
            output_tokens=usage["outputTokens"],
            latency_ms=latency_ms,
        )
