from app.solar import System, battery_plan, best_window, build_forecast, power_kw, rating


def test_power_is_zero_in_the_dark():
    s = System(kwp=3)
    assert power_kw(s, 0, 10) == 0
    assert power_kw(s, None, 10) == 0


def test_power_at_standard_conditions_equals_rating_minus_losses():
    # 1000 W/m2 and a 25 degC cell (air = 25 - 1000/800*25 = -6.25 degC)
    s = System(kwp=2, losses=0.10)
    assert power_kw(s, 1000, -6.25) == 1.8


def test_hot_panels_produce_less():
    s = System(kwp=3)
    assert power_kw(s, 800, 35) < power_kw(s, 800, 5)


def test_power_never_exceeds_the_array_rating():
    s = System(kwp=1, losses=0)
    assert power_kw(s, 1400, -30) == 1.0


def test_rating_thresholds():
    assert rating(3.2) == "good"
    assert rating(1.0) == "medium"
    assert rating(0.4) == "poor"


def _hours(values, date="2026-06-01"):
    return [{"time": f"{date}T{h:02d}:00", "power_kw": v} for h, v in enumerate(values)]


def test_best_window_picks_the_three_strongest_consecutive_hours():
    values = [0] * 9 + [0.5, 1.0, 2.0, 2.5, 2.2, 1.0] + [0] * 9
    w = best_window(_hours(values))
    # hours stamped 11:00, 12:00, 13:00 cover 10:00-13:00
    assert w == {"from": "10:00", "to": "13:00", "kwh": 6.7}


def test_best_window_is_none_on_a_dark_day():
    assert best_window(_hours([0] * 24)) is None


def test_battery_fills_and_reports_the_hour():
    s = System(kwp=3, battery_kwh=2)
    plan = battery_plan(s, _hours([0] * 9 + [1.0, 1.0, 1.0] + [0] * 12))
    # 0.9 kWh stored per hour: full during the third hour
    assert plan == {"charged_pct": 100, "full_at": "11:00"}


def test_battery_partial_charge():
    s = System(kwp=3, battery_kwh=10)
    plan = battery_plan(s, _hours([0] * 9 + [1.0, 1.0] + [0] * 13))
    assert plan == {"charged_pct": 18, "full_at": None}


def test_no_battery_no_plan():
    assert battery_plan(System(kwp=3), _hours([1.0] * 24)) is None


def _raw(days=2):
    time, gti, temp, cloud = [], [], [], []
    for d in range(days):
        for h in range(24):
            time.append(f"2026-06-{d + 1:02d}T{h:02d}:00")
            sun = 600.0 * (d + 1) if 9 <= h <= 14 else 0.0
            gti.append(min(sun, 1000.0))
            temp.append(20.0)
            cloud.append(30)
    return {
        "hourly": {"time": time, "global_tilted_irradiance": gti, "temperature_2m": temp, "cloud_cover": cloud},
        "daily": {
            "time": [f"2026-06-{d + 1:02d}" for d in range(days)],
            "sunrise": [f"2026-06-{d + 1:02d}T04:50" for d in range(days)],
            "sunset": [f"2026-06-{d + 1:02d}T21:05" for d in range(days)],
        },
    }


def test_build_forecast_sums_days_and_totals():
    f = build_forecast(System(kwp=2, losses=0.14, battery_kwh=4, tariff=4.32), _raw())
    assert len(f["hourly"]) == 48 and len(f["days"]) == 2
    d1, d2 = f["days"]
    assert d1["kwh"] == round(sum(h["power_kw"] for h in f["hourly"][:24]), 2)
    assert d2["kwh"] > d1["kwh"]
    assert f["totals"]["kwh"] == round(d1["kwh"] + d2["kwh"], 2)
    assert f["totals"]["best_day"] == "2026-06-02"
    assert f["totals"]["savings_uah"] == round(f["totals"]["kwh"] * 4.32, 2)
    assert d1["sunrise"] == "04:50" and d1["cloud_daylight_pct"] == 30
    assert d1["best_window"]["from"] == "08:00"
    assert d2["battery"]["charged_pct"] == 100
