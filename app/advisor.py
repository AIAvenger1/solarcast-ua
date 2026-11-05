"""Plain-language advice for the forecast.

Primary path: Claude API (Messages API with a structured output, so the answer
always matches the `Advice` schema and the frontend never parses free text).

Fallback path: deterministic rules that fill the same schema. It is used when
the AI layer is switched off, has no credentials, is rate-limited, declines,
or is unreachable - the service keeps working without the model.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date as _date
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger("solarcast.advisor")

MODEL = "claude-opus-5-5"
# Server-side refusal fallback: if a safety classifier declines the request,
# the API re-runs it on the model Anthropic recommends for that category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TIMEOUT_S = 60.0
# Hard ceiling on paid model calls per day for the whole service; past it the rules answer.
DAILY_AI_CALLS = int(os.environ.get("SOLARCAST_AI_DAILY_CALLS", "300"))
_ai_calls = {"day": None, "count": 0}


def _within_daily_budget() -> bool:
    today = _date.today()
    if _ai_calls["day"] != today:
        _ai_calls.update(day=today, count=0)
    if _ai_calls["count"] >= DAILY_AI_CALLS:
        return False
    _ai_calls["count"] += 1
    return True

Verdict = Literal["good", "medium", "poor"]


class DayTip(BaseModel):
    date: str = Field(description="Дата у форматі YYYY-MM-DD, як у вхідних даних")
    verdict: Verdict
    tip: str = Field(description="Одне речення: що робити цього дня")


class ApplianceSlot(BaseModel):
    appliance: str = Field(description="Прилад або дія, наприклад «Пральна машина»")
    when: str = Field(description="День і проміжок часу, наприклад «субота, 11:00–14:00»")
    why: str = Field(description="Коротке пояснення з опорою на цифри прогнозу")


class Advice(BaseModel):
    headline: str = Field(description="Головний висновок на тиждень, до 12 слів")
    summary: str = Field(description="2–3 речення: скільки енергії чекати і коли")
    days: list[DayTip]
    appliance_plan: list[ApplianceSlot] = Field(description="3–5 пунктів")
    warnings: list[str] = Field(description="Застереження; порожній список, якщо їх немає")


SYSTEM_PROMPT = """Ти — порадник з домашньої сонячної енергетики для українських домогосподарств.
Тобі дають прогноз вироблення невеликої сонячної станції на тиждень, розрахований із даних
Open-Meteo. Склади практичний план українською мовою: коли заряджати акумулятор, коли вмикати
енергоємні прилади, а в які дні розраховувати на мережу чи запас.

Правила:
- Спирайся лише на числа з вхідних даних. Не вигадуй значень, яких там немає.
- Називай конкретні дні тижня та години з поля best_window.
- Якщо потужність станції мала для приладу (наприклад, бойлер 2 кВт при піку 0,6 кВт), скажи це прямо
  і запропонуй, що реально живити.
- Якщо є акумулятор, врахуй поле battery: чи зарядиться він повністю і о котрій.
- Пиши просто, без канцеляриту та без технічного жаргону."""


def _user_prompt(payload: dict) -> str:
    return (
        "Ось прогноз у форматі JSON. Поверни план у заданій структурі.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )


_client: anthropic.AsyncAnthropic | None = None


def ai_enabled() -> bool:
    return os.environ.get("SOLARCAST_AI", "on").lower() not in ("off", "0", "false")


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        # Credentials are resolved by the SDK from the environment
        # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile).
        _client = anthropic.AsyncAnthropic(timeout=TIMEOUT_S, max_retries=1)
    return _client


async def ai_advice(payload: dict) -> Advice:
    """One Messages API call with the Advice schema as the output format."""
    response = await _get_client().beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        # a one-screen summary of a small table: low effort keeps the UI responsive
        output_config={"effort": "low"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_prompt(payload)}],
        output_format=Advice,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("модель відхилила запит")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError("модель не повернула повну структуру")
    return response.parsed_output


WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]


def _weekday(date: str) -> str:
    return WEEKDAYS[_date.fromisoformat(date).weekday()]


def _n(value: float, digits: int = 1) -> str:
    """Number the way it is written in Ukrainian: 7.13 -> 7,1"""
    return f"{value:.{digits}f}".replace(".", ",")


def rule_advice(payload: dict) -> Advice:
    """Deterministic advice built from the same payload (no model involved)."""
    days = payload["days"]
    system = payload["system"]
    total = payload["totals"]["kwh"]
    ranked = sorted(days, key=lambda d: d["kwh"], reverse=True)
    best, worst = ranked[0], ranked[-1]

    tips = []
    for d in days:
        w = d.get("best_window")
        if d["rating"] == "good" and w:
            tip = f"Сонячний день: плануйте енергоємні справи на {w['from']}–{w['to']}."
        elif d["rating"] == "medium" and w:
            tip = f"Помірне вироблення: одного приладу за раз вистачить у {w['from']}–{w['to']}."
        else:
            tip = "Хмарно: розраховуйте на мережу або запас акумулятора."
        tips.append(DayTip(date=d["date"], verdict=d["rating"], tip=tip))

    plan = []
    for d, appliance in zip(ranked[:3], ["Пральна машина", "Зарядка акумулятора та павербанків", "Праска або пилосос"]):
        w = d.get("best_window")
        if not w:
            continue
        plan.append(ApplianceSlot(
            appliance=appliance,
            when=f"{_weekday(d['date'])}, {w['from']}–{w['to']}",
            why=f"За ці години очікується {_n(w['kwh'])} кВт·год, пік {_n(d['peak_kw'], 2)} кВт.",
        ))

    warnings = []
    if best["peak_kw"] < 1.5:
        warnings.append(
            f"Пікова потужність не перевищить {_n(best['peak_kw'], 2)} кВт: бойлер чи електроплиту "
            "станція сама не потягне.")
    if system.get("battery_kwh"):
        short = [d for d in days if d.get("battery") and d["battery"]["charged_pct"] < 100]
        if short:
            warnings.append(
                f"Акумулятор не зарядиться повністю {len(short)} з {len(days)} днів.")
    if worst["rating"] == "poor":
        warnings.append(f"Найслабший день — {_weekday(worst['date'])}: лише {_n(worst['kwh'])} кВт·год.")

    return Advice(
        headline=f"Найкращий день — {_weekday(best['date'])}: {_n(best['kwh'])} кВт·год",
        summary=(
            f"За тиждень станція {_n(system['kwp'])} кВт ({payload['location']['name']}) "
            f"дасть близько {_n(total)} кВт·год. Найбільше — {_weekday(best['date'])}, "
            f"найменше — {_weekday(worst['date'])}."),
        days=tips,
        appliance_plan=plan,
        warnings=warnings,
    )


async def get_advice(payload: dict) -> dict:
    """Advice plus where it came from. Never raises: the rules are the safety net.

    `note` is shown to the user, so it only ever holds one of the fixed messages
    below; exception details go to the server log.
    """
    if not ai_enabled():
        note = "ШІ-порадника вимкнено"
    elif not _within_daily_budget():
        note = "вичерпано денний ліміт запитів до моделі"
    else:
        try:
            advice = await ai_advice(payload)
            return {"source": "claude", "model": MODEL, "note": None, "advice": advice.model_dump()}
        except anthropic.AuthenticationError:
            note = "ключ Claude API недійсний"
        except anthropic.RateLimitError:
            note = "перевищено ліміт запитів Claude API"
        except anthropic.APIStatusError as e:
            log.warning("Claude API status error %s", e.status_code)
            note = "Claude API тимчасово недоступний"
        except anthropic.APIConnectionError:
            note = "немає з'єднання з Claude API"
        except Exception as e:
            # with no credentials at all the SDK raises before any request is sent
            if "authentication method" in str(e):
                note = "не задано ключ Claude API (змінна ANTHROPIC_API_KEY)"
            else:
                log.exception("AI advice failed")
                note = "ШІ-порадник тимчасово недоступний"
        log.warning("AI advice unavailable, using rules: %s", note)
    return {"source": "rules", "model": None, "note": note, "advice": rule_advice(payload).model_dump()}
