"""Where the agent's spans go besides Grafana: the Langfuse project (ADR-027)."""

from base64 import b64encode

from assistant_api import main
from assistant_api.settings import get_settings
from tests.integration.test_turns import PUBLIC_PEM


async def test_with_the_langfuse_keys_the_agent_spans_go_to_the_project(monkeypatch):
    added: list = []
    monkeypatch.setenv("JWT_PUBLIC_KEY", PUBLIC_PEM)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # Grafana's token, which the exporter also reads from the environment.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic grafana")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-2")
    monkeypatch.setattr(main, "export_to_langfuse", added.append)
    get_settings.cache_clear()

    async with main.app.router.lifespan_context(main.app):
        pass

    get_settings.cache_clear()
    [exporter] = added
    assert exporter._client._endpoint == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    headers = exporter._client._headers
    assert headers["authorization"] == "Basic " + b64encode(b"pk-lf-1:sk-lf-2").decode()
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert "grafana" not in str(headers)


async def test_without_the_langfuse_keys_nothing_goes_there(monkeypatch):
    added: list = []
    monkeypatch.setenv("JWT_PUBLIC_KEY", PUBLIC_PEM)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(main, "export_to_langfuse", added.append)
    get_settings.cache_clear()

    async with main.app.router.lifespan_context(main.app):
        pass

    get_settings.cache_clear()
    assert added == []
