from fastapi import FastAPI

app = FastAPI(title="UdeSA-X Assistant API", version="0.1.0")


@app.get("/healthcheck", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Nothing to check yet: the service opens no connections until it gets its endpoint."""
    return {"status": "ok"}
