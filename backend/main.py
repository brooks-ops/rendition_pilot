"""Minimal FastAPI app scaffold for future backend endpoints."""

from fastapi import FastAPI


app = FastAPI(title="Rendition Pilot API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "rendition-pilot-api"}
