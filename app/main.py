"""SolarCast UA - HTTP API and static frontend.

    GET  /api/geocode?q=Київ                      place search (Open-Meteo Geocoding API)
    GET  /api/forecast?lat=..&lon=..&kwp=..       7-day PV forecast (Open-Meteo Forecast API + PV model)
    POST /api/advice                              plain-language plan (Claude API, rule-based fallback)
    GET  /api/health                              liveness + cache stats
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import advisor, openmeteo
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
    return {"status": "ok", "ai_enabled": advisor.ai_enabled(), "cache": openmeteo.cache_stats()}


@app.get("/api/geocode")
async def geocode(request: Request, q: str = Query(min_length=2, max_length=80)):
    try:
        return {"results": await openmeteo.geocode(request.app.state.http, q.strip())}
    except openmeteo.UpstreamError as e:
        raise HTTPException(502, str(e))


@app.get("/api/forecast")
async def forecast(
    request: Request,
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    kwp: float = Query(gt=0, le=100, description="installed peak power, kWp"),
    tilt: float = Query(35, ge=0, le=90, description="panel tilt, degrees from horizontal"),
    azimuth: float = Query(0, ge=-180, le=180, description="0 = south, -90 = east, 90 = west"),
    losses: float = Query(14, ge=0, le=50, description="system losses, %"),
    battery_kwh: float = Query(0, ge=0, le=200),
    tariff: float = Query(4.32, ge=0, le=100, description="UAH per kWh"),
    name: str = Query("", max_length=120),
):
    try:
        raw, cached = await openmeteo.forecast(request.app.state.http, lat, lon, tilt, azimuth)
    except openmeteo.UpstreamError as e:
        raise HTTPException(502, str(e))
    system = System(kwp=kwp, losses=losses / 100.0, battery_kwh=battery_kwh, tariff=tariff)
    result = build_forecast(system, raw)
    return {
        "location": {
            "name": name or f"{lat:.2f}, {lon:.2f}",
            "lat": raw["latitude"], "lon": raw["longitude"],
            "timezone": raw.get("timezone"), "elevation": raw.get("elevation"),
        },
        "system": {"kwp": kwp, "tilt": tilt, "azimuth": azimuth, "losses_pct": losses,
                   "battery_kwh": battery_kwh, "tariff": tariff},
        "source": {"provider": "Open-Meteo", "cached": cached},
        **result,
    }


class AdviceRequest(BaseModel):
    """The parts of a forecast the advisor needs (hourly data is left out on purpose)."""
    location: dict
    system: dict
    days: list[dict] = Field(min_length=1, max_length=16)
    totals: dict


@app.post("/api/advice")
async def advice(body: AdviceRequest):
    try:
        return await advisor.get_advice(body.model_dump())
    except (KeyError, ValueError, IndexError) as e:
        raise HTTPException(422, f"некоректні дані прогнозу: {e}")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
