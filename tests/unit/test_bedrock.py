"""What the Bedrock model sends and how it reads the answer, against a fake client."""

from assistant_api.agent.bedrock import MAX_TOKENS, BedrockModel


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def converse(self, **request) -> dict:
        self.requests.append(request)
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": "hola"}]}},
            "usage": {"inputTokens": 120, "outputTokens": 8, "totalTokens": 128},
        }


def test_the_request_has_a_token_ceiling_and_forces_a_tool():
    client = FakeClient()
    model = BedrockModel("arn:modelo", region="us-east-1", client=client)

    turn = model.converse("sistema", [{"role": "user", "content": [{"text": "x"}]}], [{"t": 1}])

    request = client.requests[0]
    assert request["modelId"] == "arn:modelo"
    assert request["system"] == [{"text": "sistema"}]
    assert request["inferenceConfig"] == {"maxTokens": MAX_TOKENS}
    assert request["toolConfig"] == {"tools": [{"t": 1}], "toolChoice": {"any": {}}}
    assert (turn.content, turn.input_tokens, turn.output_tokens) == ([{"text": "hola"}], 120, 8)
    assert turn.latency_ms >= 0


def test_thinking_is_off_unless_asked_for():
    off, on = FakeClient(), FakeClient()

    BedrockModel("arn", region="us-east-1", client=off).converse("s", [], [])
    BedrockModel("arn", region="us-east-1", client=on, thinking=True).converse("s", [], [])

    assert off.requests[0]["additionalModelRequestFields"] == {"thinking": {"type": "disabled"}}
    assert "additionalModelRequestFields" not in on.requests[0]


def test_the_real_client_never_retries():
    model = BedrockModel("arn", region="us-east-1")

    assert model.client.meta.region_name == "us-east-1"
    assert model.client.meta.config.retries["total_max_attempts"] == 1
