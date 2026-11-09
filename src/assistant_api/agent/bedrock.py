"""The model behind the loop: any Bedrock model that does tool use through Converse.

Which model is configuration (`BEDROCK_MODEL_ID`, the ARN of the group's
inference profile). Credentials are never here: locally they come from
`aws login --profile tds-group-3`, and in the cluster from the pod's role.
"""

import time

import boto3
from botocore.config import Config

from assistant_api.agent.loop import ModelTurn

# An action is a few dozen tokens and the spoken answer a sentence: this is
# room to spare, and a ceiling on what a runaway answer can cost.
MAX_TOKENS = 400


class BedrockModel:
    def __init__(
        self, model_id: str, *, region: str, thinking: bool = False, client: object = None
    ) -> None:
        self.model_id = model_id
        # Off by default: thinking delays the first action by seconds, and picking
        # one of a dozen actions does not need it.
        self.thinking = thinking
        # No retries: a throttled call fails at once instead of turning into
        # several calls against the group's monthly quota.
        self.client = client or boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(retries={"total_max_attempts": 1, "mode": "standard"}),
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
        response = self.client.converse(**request)
        latency_ms = (time.perf_counter() - started) * 1000
        usage = response["usage"]
        return ModelTurn(
            content=response["output"]["message"]["content"],
            input_tokens=usage["inputTokens"],
            output_tokens=usage["outputTokens"],
            latency_ms=latency_ms,
        )
