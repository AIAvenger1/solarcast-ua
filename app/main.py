"""SolarCast UA - HTTP API and static frontend.

    GET  /api/geocode?q=Київ                      place search (Open-Meteo Geocoding API)
    GET  /api/forecast?lat=..&lon=..&kwp=..       7-day PV forecast (Open-Meteo Forecast API + PV model)
    POST /api/advice  {station parameters}        plain-language plan (Claude API, rule-based fallback)
    GET  /api/health                              liveness + cache stats
"""
from __future__ import annotations

import hmac
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import advisor, openmeteo
from .limits import WindowLimiter
from .solar import System, build_forecast

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # one shared connection pool for all upstream calls
    async with httpx.AsyncClient(headers={"User-Agent": "solarcast-ua/0.1 (student PoC)"}) as client:
        app.state.http = client
        yield


app = FastAPI(title="SolarCast UA", version="0.1.0", lifespan=lifespan)


@app.get("/api/health")
async def health():
    return {"status": "ok", "ai_enabled": advisor.ai_enabled(), "ai_requires_code": bool(access_code()),
            "cache": openmeteo.cache_stats()}


@app.get("/api/geocode")
async def geocode(request: Request, q: str = Query(min_length=2, max_length=80)):
    try:
        return {"results": await openmeteo.geocode(request.app.state.http, q.strip())}
    except openmeteo.UpstreamError as e:
        raise HTTPException(502, str(e))


class Station(BaseModel):
    """Everything the client may send: a place and the parameters of the PV system.

    The same model is the query of GET /api/forecast and the body of POST /api/advice,
    so the advisor never sees data the server did not compute itself.
    """
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    kwp: float = Field(gt=0, le=100, description="installed peak power, kWp")
    tilt: float = Field(35, ge=0, le=90, description="panel tilt, degrees from horizontal")
    azimuth: float = Field(0, ge=-180, le=180, description="0 = south, -90 = east, 90 = west")
    losses: float = Field(14, ge=0, le=50, description="system losses, %")
    battery_kwh: float = Field(0, ge=0, le=200)
    tariff: float = Field(4.32, ge=0, le=100, description="UAH per kWh")
    name: str = Field("", max_length=80)

    @field_validator("name")
    @classmethod
    def plain_place_name(cls, v: str) -> str:
        # The name is echoed in the UI and reaches the model prompt: keep letters,
        # digits and ordinary punctuation only, so it cannot carry markup or instructions.
        return re.sub(r"[^\w\s,.'’\-()]", "", v, flags=re.UNICODE).strip()


async def compute(request: Request, q: Station) -> dict:
    try:
        raw, cached = await openmeteo.forecast(request.app.state.http, q.lat, q.lon, q.tilt, q.azimuth)
    except openmeteo.UpstreamError as e:
        raise HTTPException(502, str(e))
    system = System(kwp=q.kwp, losses=q.losses / 100.0, battery_kwh=q.battery_kwh, tariff=q.tariff)
    return {
        "location": {
            "name": q.name or f"{q.lat:.2f}, {q.lon:.2f}",
            "lat": raw["latitude"], "lon": raw["longitude"],
            "timezone": raw.get("timezone"), "elevation": raw.get("elevation"),
        },
        "system": {"kwp": q.kwp, "tilt": q.tilt, "azimuth": q.azimuth, "losses_pct": q.losses,
                   "battery_kwh": q.battery_kwh, "tariff": q.tariff},
        "source": {"provider": "Open-Meteo", "cached": cached},
        **build_forecast(system, raw),
    }


@app.get("/api/forecast")
async def forecast(request: Request, q: Annotated[Station, Query()]):
    return await compute(request, q)


# /api/advice can spend money on the model, so it is rate-limited per client address
# (see app/limits.py for what "per client" means in this PoC).
ADVICE_LIMIT = 6
ADVICE_WINDOW_S = 10 * 60
advice_limiter = WindowLimiter(ADVICE_LIMIT, ADVICE_WINDOW_S)


def access_code() -> str:
    """Optional shared secret that unlocks the paid model on a public deployment."""
    return os.environ.get("SOLARCAST_ACCESS_TOKEN", "").strip()


@app.post("/api/advice")
async def advice(request: Request, q: Station):
    client = request.client.host if request.client else "unknown"
    # the limiter runs before the code check, so guessing the code is rate-limited too
    if not advice_limiter.allow(client):
        raise HTTPException(429, "Забагато запитів плану. Спробуйте за кілька хвилин.")
    required, given = access_code(), request.headers.get("x-access-token", "").strip()
    if required and given and not hmac.compare_digest(given.encode(), required.encode()):
        raise HTTPException(401, "Невірний код доступу.")
    # Without the code the visitor still gets a plan, built by the free rules.
    use_ai = bool(given) if required else True
    # The forecast is recomputed here (Open-Meteo answers come from the cache), so the
    # model only ever receives numbers this service produced. Hourly data is left out.
    f = await compute(request, q)
    payload = {k: f[k] for k in ("location", "system", "days", "totals")}
    return await advisor.get_advice(payload, client, use_ai=use_ai)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
