"""Operator-note interpretation via a language-capable generative model.

This is the ONLY module permitted to decide what an operator note means. It is
deliberately thin: build a prompt, call the model, parse JSON, hand the result
back to the guardrail layer. No directive logic lives here, because nothing the
model returns is trusted until :mod:`app.guardrails` has validated it.

The model is reached over the Anthropic Messages API shape. ``LLM_API_STYLE``
switches to an OpenAI-compatible shape, which is what a local Ollama or most
other providers speak — useful as a fallback route.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("gridwise.interpret")

SYSTEM_PROMPT = """You are the operator-note interpretation stage of a campus \
energy scheduling system.

You receive short natural-language notes written by campus facilities operators, \
plus the physical parameters of the campus battery. Convert each note into \
exactly ONE structured directive that a deterministic optimizer can apply.

SUPPORTED DIRECTIVES — choose exactly one per note:

1. solar_reduction — usable solar is reduced during specific hours.
   structured_adjustment: {"hours": [...], "factor": <number 0..1>}
   "factor" is the fraction of normal solar that REMAINS usable.
     "solar drops to 20%"                 -> factor 0.2
     "an 80% reduction in solar"          -> factor 0.2   (80% LOST, 20% remains)
     "about half the normal output"       -> factor 0.5
     "roughly one-fifth of normal output" -> factor 0.2

2. minimum_battery_reserve — the battery must stay at or above a level.
   structured_adjustment: {"hours": [...], "minimum_energy_kwh": <number>}
   If the note states a PERCENTAGE, convert it using the battery capacity given.
     capacity 200 kWh, "keep at least 50% in reserve" -> minimum_energy_kwh 100

3. no_charge_window — battery charging is unavailable during these hours.
   structured_adjustment: {"hours": [...]}

4. no_discharge_window — battery discharging is unavailable during these hours.
   structured_adjustment: {"hours": [...]}

5. max_grid_window — grid import may not exceed a stated amount in these hours.
   structured_adjustment: {"hours": [...], "max_grid_kwh": <number>}

6. no_op — the note does not affect the 24-hour energy schedule.
   structured_adjustment: null

TIME WINDOWS — READ CAREFULLY
Use 24-hour clock integers 0 through 23. The START hour is INCLUDED and the END
hour is EXCLUDED.
  "1 PM to 3 PM"        -> [13, 14]
  "noon until 2 PM"     -> [12, 13]
  "from 6 PM until 9 PM"-> [18, 19, 20]
  "2 AM until 5 AM"     -> [2, 3, 4]
  "11 AM until 1 PM"    -> [11, 12]
Hours must be unique and in ascending order.

RULES
- Return EXACTLY one entry per note, with note_index equal to that note's
  0-based position, in the same order.
- A note about anything other than these five energy concerns (cafeteria menus,
  library opening hours, sports registration, seminars, office announcements,
  anything unrelated to this 24-hour electricity schedule) is no_op.
- Never invent a directive. If a note is ambiguous or does not clearly map to
  one of the five, use no_op.
- Copy or convert numeric values exactly as described.

OUTPUT
Return ONLY a JSON array. No prose, no markdown code fences.
[{"note_index":0,"applies":true,"directive_type":"solar_reduction",\
"structured_adjustment":{"hours":[13,14],"factor":0.2},"explanation":"short reason"}]
For an irrelevant note:
[{"note_index":1,"applies":false,"directive_type":"no_op",\
"structured_adjustment":null,"explanation":"short reason"}]"""

USER_TEMPLATE = """Battery parameters for this scenario:
{battery}

Operator notes:
{notes}

Return the JSON array."""


class InterpretationError(RuntimeError):
    """Raised when the model could not be reached or returned unusable output."""


def _settings() -> dict[str, Any]:
    return {
        "base_url": os.environ.get("LLM_BASE_URL", "https://capi.aerolink.lat").rstrip("/"),
        "api_key": os.environ.get("LLM_API_KEY", "").strip(),
        "model": os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001").strip(),
        "style": os.environ.get("LLM_API_STYLE", "anthropic").strip().lower(),
        "timeout": float(os.environ.get("LLM_TIMEOUT_SECONDS", "20")),
        "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "1500")),
    }


def is_configured() -> bool:
    """True when an API key is present. The service still runs without one,
    but every note degrades to no_op."""
    return bool(_settings()["api_key"])


def describe_route() -> str:
    settings = _settings()
    return f"{settings['style']} :: {settings['model']} @ {settings['base_url']}"


def _build_user_message(notes: list[str], battery: dict[str, float]) -> str:
    battery_lines = "\n".join(f"- {key}: {value}" for key, value in battery.items())
    note_lines = "\n".join(f"{index}: {note}" for index, note in enumerate(notes))
    return USER_TEMPLATE.format(battery=battery_lines, notes=note_lines)


def _post_anthropic(
    settings: dict[str, Any], user_message: str, client: httpx.Client
) -> str:
    response = client.post(
        f"{settings['base_url']}/v1/messages",
        headers={
            "x-api-key": settings["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": settings["model"],
            "max_tokens": settings["max_tokens"],
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        },
    )
    response.raise_for_status()
    payload = response.json()
    blocks = payload.get("content") or []
    text = "".join(
        block.get("text", "") for block in blocks if isinstance(block, dict)
    )
    if not text.strip():
        raise InterpretationError("model returned no text content")
    return text


def _post_openai(
    settings: dict[str, Any], user_message: str, client: httpx.Client
) -> str:
    response = client.post(
        f"{settings['base_url']}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "content-type": "application/json",
        },
        json={
            "model": settings["model"],
            "max_tokens": settings["max_tokens"],
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        },
    )
    response.raise_for_status()
    payload = response.json()
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise InterpretationError(f"unexpected chat-completion shape: {exc}") from exc


def extract_json_array(text: str) -> list[Any]:
    """Pull a JSON array out of raw model text.

    Models occasionally wrap output in code fences or add a sentence of
    preamble even when told not to. Recover rather than fail.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise InterpretationError("no JSON array found in model output")
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise InterpretationError(f"model output was not valid JSON: {exc}") from exc

    if isinstance(parsed, dict):
        # Some models wrap the array in an object; unwrap common shapes.
        for key in ("directive_interpretation", "directives", "interpretations", "result"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        raise InterpretationError("model returned an object, not an array")

    if not isinstance(parsed, list):
        raise InterpretationError(f"expected a JSON array, got {type(parsed).__name__}")

    return parsed


def interpret_notes(
    notes: list[str],
    battery: dict[str, float],
    client: httpx.Client | None = None,
) -> list[Any]:
    """Ask the model to interpret every operator note.

    Returns the raw, UNVALIDATED list from the model. Callers must pass it
    through :func:`app.guardrails.validate_interpretation` before use.

    Raises :class:`InterpretationError` if the model is unreachable or returns
    something that cannot be parsed as a JSON array. The caller is expected to
    degrade gracefully rather than propagate this to the client.
    """
    settings = _settings()
    if not settings["api_key"]:
        raise InterpretationError("LLM_API_KEY is not set")

    user_message = _build_user_message(notes, battery)
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=settings["timeout"])

    try:
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                if settings["style"] == "openai":
                    text = _post_openai(settings, user_message, client)
                else:
                    text = _post_anthropic(settings, user_message, client)
                return extract_json_array(text)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                body = exc.response.text[:300]
                logger.warning(
                    "LLM provider returned %s on attempt %d: %s", status, attempt, body
                )
                # 4xx other than 429 will not improve on retry.
                if status != 429 and 400 <= status < 500:
                    break
            except (httpx.HTTPError, InterpretationError, ValueError) as exc:
                last_error = exc
                logger.warning("LLM attempt %d failed: %s", attempt, exc)

        raise InterpretationError(str(last_error) if last_error else "LLM call failed")
    finally:
        if owns_client:
            client.close()
