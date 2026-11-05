"""Thin async client for the two Open-Meteo APIs the service uses.

Both are public HTTPS APIs that need no key for non-commercial use:
  * Geocoding API  - place name -> coordinates, timezone
  * Forecast API   - hourly irradiance on a tilted plane, temperature, cloud cover

Responses are cached in memory for a few minutes: the forecast is refreshed
hourly upstream, so repeating the same request more often only adds latency
and load on a free service.
"""
from __future__ import annotations

import time

import httpx

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL_S = 15 * 60
TIMEOUT_S = 15.0

HOURLY = "global_tilted_irradiance,temperature_2m,cloud_cover"
DAILY = "sunrise,sunset"


class UpstreamError(Exception):
    """Open-Meteo is unreachable or answered with an error."""


_cache: dict[tuple, tuple[float, dict]] = {}


def cache_stats() -> dict:
    return {"entries": len(_cache), "ttl_s": CACHE_TTL_S}


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> tuple[dict, bool]:
    """GET with a TTL cache. Returns (json, served_from_cache)."""
    key = (url, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1], True
    try:
        r = await client.get(url, params=params, timeout=TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        # Open-Meteo explains bad parameters as {"error": true, "reason": "..."}
        try:
            reason = e.response.json().get("reason", "")
        except ValueError:
            reason = ""
        raise UpstreamError(f"Open-Meteo {e.response.status_code}: {reason}".strip()) from e
    except (httpx.HTTPError, ValueError) as e:
        raise UpstreamError(f"Open-Meteo недоступний: {type(e).__name__}") from e
    _cache[key] = (time.monotonic(), data)
    return data, False


async def geocode(client: httpx.AsyncClient, query: str, count: int = 6) -> list[dict]:
    data, _ = await _get(client, GEOCODING_URL, {
        "name": query, "count": count, "language": "uk", "format": "json",
    })
    return [
        {
            "name": r["name"],
            "region": r.get("admin1"),
            "country": r.get("country"),
            "country_code": r.get("country_code"),
            "lat": r["latitude"],
            "lon": r["longitude"],
            "timezone": r.get("timezone"),
        }
        for r in data.get("results", [])
    ]


async def forecast(client: httpx.AsyncClient, lat: float, lon: float,
                   tilt: float, azimuth: float, days: int = 7) -> tuple[dict, bool]:
    """Hourly irradiance on the panel plane. Azimuth: 0 = south, -90 = east, 90 = west."""
    return await _get(client, FORECAST_URL, {
        # 2 decimals is ~1 km: finer than the weather model grid, and it makes
        # nearby requests share one cache entry
        "latitude": round(lat, 2), "longitude": round(lon, 2),
        "hourly": HOURLY, "daily": DAILY,
        "tilt": tilt, "azimuth": azimuth,
        "timezone": "auto", "forecast_days": days,
    })
