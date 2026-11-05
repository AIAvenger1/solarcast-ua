"""PV output model: turns Open-Meteo irradiance into expected kWh.

Pure functions, no I/O, so the whole model is unit-testable offline.

Model (the standard "performance ratio" estimate used for small rooftop systems):

    P_ac [kW] = P_peak [kWp] * GTI / 1000 * (1 - losses) * (1 + gamma * (T_cell - 25))
    T_cell    = T_air + GTI / 800 * (NOCT - 20)

GTI is the global tilted irradiance (W/m2) on the panel plane, which Open-Meteo
computes for the requested tilt and azimuth. Hourly values are means over the
preceding hour, so one hourly value in kW is also that hour's energy in kWh.
"""
from __future__ import annotations

from dataclasses import dataclass

STC_IRRADIANCE = 1000.0      # W/m2, irradiance at which a panel delivers its rated kWp
TEMP_COEFF = -0.004          # 1/degC, typical power temperature coefficient of c-Si panels
NOCT = 45.0                  # degC, nominal operating cell temperature
CHARGE_EFFICIENCY = 0.90     # share of PV energy that ends up stored in the battery
WINDOW_HOURS = 3             # length of the "best window" suggested for heavy loads


@dataclass(frozen=True)
class System:
    kwp: float                # installed peak power, kWp
    losses: float = 0.14      # inverter + wiring + soiling, 0..1
    battery_kwh: float = 0.0  # 0 = no battery
    tariff: float = 4.32      # UAH per kWh, for the savings estimate


def cell_temperature(air_temp_c: float, gti: float) -> float:
    return air_temp_c + gti / 800.0 * (NOCT - 20.0)


def power_kw(system: System, gti: float | None, air_temp_c: float | None) -> float:
    """AC power for one hour. Missing data counts as darkness."""
    if not gti or gti <= 0:
        return 0.0
    t_air = 20.0 if air_temp_c is None else air_temp_c
    temp_factor = 1.0 + TEMP_COEFF * (cell_temperature(t_air, gti) - 25.0)
    p = system.kwp * gti / STC_IRRADIANCE * (1.0 - system.losses) * temp_factor
    # an inverter cannot deliver more than the array's rating
    return round(max(0.0, min(p, system.kwp)), 3)


def rating(specific_yield: float) -> str:
    """kWh per installed kWp per day -> coarse verdict."""
    if specific_yield >= 3.0:
        return "good"
    if specific_yield >= 1.0:
        return "medium"
    return "poor"


def best_window(hours: list[dict]) -> dict | None:
    """Contiguous WINDOW_HOURS block with the most energy (when to run heavy loads)."""
    if len(hours) < WINDOW_HOURS:
        return None
    best_i, best_e = 0, -1.0
    for i in range(len(hours) - WINDOW_HOURS + 1):
        e = sum(h["power_kw"] for h in hours[i:i + WINDOW_HOURS])
        if e > best_e:
            best_i, best_e = i, e
    if best_e <= 0:
        return None
    # an hourly value covers the hour that ENDS at its timestamp
    start = hours[best_i]["time"][11:13]
    end = hours[best_i + WINDOW_HOURS - 1]["time"][11:13]
    return {"from": f"{(int(start) - 1) % 24:02d}:00", "to": f"{int(end):02d}:00", "kwh": round(best_e, 2)}


def battery_plan(system: System, hours: list[dict]) -> dict | None:
    """Charge an empty battery from sunrise: how full does it get, and when is it full."""
    if system.battery_kwh <= 0:
        return None
    stored = 0.0
    full_at = None
    for h in hours:
        stored += h["power_kw"] * CHARGE_EFFICIENCY
        if stored >= system.battery_kwh and full_at is None:
            full_at = h["time"][11:16]
    return {
        "charged_pct": round(min(100.0, stored / system.battery_kwh * 100.0)),
        "full_at": full_at,
    }


def build_forecast(system: System, raw: dict) -> dict:
    """Open-Meteo forecast JSON -> hourly power, per-day summary, week totals."""
    h = raw["hourly"]
    hourly = []
    for i, t in enumerate(h["time"]):
        gti = h["global_tilted_irradiance"][i]
        temp = h["temperature_2m"][i]
        hourly.append({
            "time": t,
            "gti": gti,
            "temp": temp,
            "cloud": h["cloud_cover"][i],
            "power_kw": power_kw(system, gti, temp),
        })

    d = raw["daily"]
    days = []
    for i, date in enumerate(d["time"]):
        day_hours = [x for x in hourly if x["time"].startswith(date)]
        kwh = round(sum(x["power_kw"] for x in day_hours), 2)
        peak = max(day_hours, key=lambda x: x["power_kw"], default=None)
        clouds = [x["cloud"] for x in day_hours if x["power_kw"] > 0 and x["cloud"] is not None]
        days.append({
            "date": date,
            "kwh": kwh,
            "specific_yield": round(kwh / system.kwp, 2),
            "rating": rating(kwh / system.kwp),
            "peak_kw": peak["power_kw"] if peak else 0.0,
            "peak_time": peak["time"][11:16] if peak and peak["power_kw"] > 0 else None,
            "sunrise": d["sunrise"][i][11:16],
            "sunset": d["sunset"][i][11:16],
            "cloud_daylight_pct": round(sum(clouds) / len(clouds)) if clouds else None,
            "best_window": best_window(day_hours),
            "battery": battery_plan(system, day_hours),
            "savings_uah": round(kwh * system.tariff, 2),
        })

    total = round(sum(x["kwh"] for x in days), 2)
    best = max(days, key=lambda x: x["kwh"], default=None)
    worst = min(days, key=lambda x: x["kwh"], default=None)
    return {
        "hourly": hourly,
        "days": days,
        "totals": {
            "kwh": total,
            "savings_uah": round(total * system.tariff, 2),
            "avg_kwh_per_day": round(total / len(days), 2) if days else 0.0,
            "best_day": best["date"] if best else None,
            "worst_day": worst["date"] if worst else None,
        },
    }
