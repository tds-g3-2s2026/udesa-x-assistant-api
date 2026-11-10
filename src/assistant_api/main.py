from importlib.metadata import version

from fastapi import FastAPI

app = FastAPI(title="UdeSA-X Assistant API", version=version("assistant-api"))


@app.get("/healthcheck", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Nothing to check yet: the service opens no connections until it gets its endpoint."""
    return {"status": "ok"}
