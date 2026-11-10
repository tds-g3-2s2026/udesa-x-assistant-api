from httpx import ASGITransport, AsyncClient

from assistant_api.main import app


async def test_healthcheck_answers_ok():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthcheck")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
