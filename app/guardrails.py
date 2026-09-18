"""Deterministic guardrails — Problem Statement §08.

LLM output is treated as UNTRUSTED structured data. Nothing from the model
reaches the optimizer until it has passed every check in this module.

Design rule: a note whose interpretation cannot be validated is downgraded to
``no_op`` rather than silently repaired into a different directive. Downgrading
costs interpretation credit for that note but keeps the response schema-valid
and the schedule feasible — which is strictly better than crashing or inventing
a constraint the operator never asked for.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from .schemas import APPLICABLE_DIRECTIVES, DIRECTIVE_TYPES

HOUR_SET = set(range(24))

# Directive types and the numeric key each one requires.
_NUMERIC_KEY = {
    "solar_reduction": "factor",
    "minimum_battery_reserve": "minimum_energy_kwh",
    "max_grid_window": "max_grid_kwh",
}

_NO_OP_ENTRY = {
    "applies": False,
    "directive_type": "no_op",
    "structured_adjustment": None,
    "explanation": "This note does not affect today's 24-hour energy schedule.",
}


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_hours(raw: Any) -> list[int] | None:
    """Return unique ascending integers in 0..23, or None if unusable."""
    if not isinstance(raw, (list, tuple)):
        return None
    cleaned: set[int] = set()
    for item in raw:
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            value = item
        elif isinstance(item, float) and float(item).is_integer():
            value = int(item)
        else:
            return None
        if value not in HOUR_SET:
            return None
        cleaned.add(value)
    return sorted(cleaned)


def validate_entry(entry: Any, battery_capacity: float) -> tuple[dict | None, str | None]:
    """Validate one raw interpretation entry.

    Returns ``(clean_entry, None)`` on success or ``(None, reason)`` on rejection.
    """
    if not isinstance(entry, dict):
        return None, "entry is not an object"

    directive_type = entry.get("directive_type")
    if directive_type not in DIRECTIVE_TYPES:
        return None, f"unsupported directive_type {directive_type!r}"

    explanation = entry.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        explanation = "Interpretation supplied by the language model."

    # ---- no_op: the only directive permitted to use applies = false --------
    if directive_type == "no_op":
        if entry.get("applies") not in (False, None):
            # Model claimed no_op but marked it applicable — no_op still means
            # "no change", so normalise rather than reject.
            pass
        return (
            {
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": explanation,
            },
            None,
        )

    # ---- every other directive must be applicable -------------------------
    if entry.get("applies") is False:
        return None, f"{directive_type} returned with applies=false"

    adjustment = entry.get("structured_adjustment")
    if not isinstance(adjustment, dict):
        return None, f"{directive_type} missing structured_adjustment object"

    hours = normalize_hours(adjustment.get("hours"))
    if hours is None:
        return None, f"{directive_type} has invalid hours"
    if not hours:
        return None, f"{directive_type} has empty hours"

    clean_adjustment: dict[str, Any] = {"hours": hours}

    # ---- per-directive numeric validation ---------------------------------
    if directive_type in _NUMERIC_KEY:
        key = _NUMERIC_KEY[directive_type]
        value = adjustment.get(key)
        if not _is_finite_number(value):
            return None, f"{directive_type} missing finite {key}"
        value = float(value)

        if directive_type == "solar_reduction":
            # factor is the usable fraction REMAINING, so it must sit in [0, 1].
            if not (0.0 <= value <= 1.0):
                return None, f"solar_reduction factor {value} outside [0, 1]"
        elif directive_type == "minimum_battery_reserve":
            if value < 0:
                return None, "minimum_battery_reserve negative"
            if value > battery_capacity + 1e-9:
                return None, (
                    f"minimum_battery_reserve {value} exceeds capacity {battery_capacity}"
                )
        elif directive_type == "max_grid_window":
            if value < 0:
                return None, "max_grid_window negative"

        clean_adjustment[key] = value

    return (
        {
            "applies": True,
            "directive_type": directive_type,
            "structured_adjustment": clean_adjustment,
            "explanation": explanation,
        },
        None,
    )


def validate_interpretation(
    raw_entries: Any,
    note_count: int,
    battery_capacity: float,
) -> tuple[list[dict], list[str]]:
    """Coerce raw LLM output into exactly one valid entry per operator note.

    Returns ``(entries, warnings)`` where ``entries`` is always ordered
    ``0..note_count-1``. Entries that fail validation become ``no_op``.
    """
    warnings: list[str] = []

    # Index the model's entries by note_index, keeping the first well-formed
    # claim for each note. Duplicates are a schema failure per §08, so we drop
    # the extras rather than emit a superset.
    #
    # Normalisation, not interpretation: if the model omits note_index but the
    # array is in note order, its position is the index. Deterministic
    # post-processing of this kind is explicitly permitted (§04).
    by_index: dict[int, Any] = {}
    if isinstance(raw_entries, list):
        for position, item in enumerate(raw_entries):
            if not isinstance(item, dict):
                warnings.append("dropped non-object interpretation entry")
                continue

            index = item.get("note_index")
            if isinstance(index, bool):
                index = None
            elif isinstance(index, float) and index.is_integer():
                index = int(index)
            elif not isinstance(index, int):
                index = None

            if index is None:
                if position < note_count:
                    warnings.append(
                        f"note_index missing at position {position}; used array position"
                    )
                    index = position
                else:
                    warnings.append("dropped entry with no usable note_index")
                    continue

            if not (0 <= index < note_count):
                warnings.append(f"dropped entry with out-of-range note_index {index}")
                continue
            if index in by_index:
                warnings.append(f"dropped duplicate entry for note_index {index}")
                continue
            by_index[index] = item
    else:
        warnings.append("interpretation was not a list; treating all notes as no_op")

    entries: list[dict] = []
    for index in range(note_count):
        raw = by_index.get(index)
        if raw is None:
            warnings.append(f"no interpretation for note_index {index}; marked no_op")
            entries.append({"note_index": index, **_NO_OP_ENTRY})
            continue

        clean, reason = validate_entry(raw, battery_capacity)
        if clean is None:
            warnings.append(f"note_index {index} rejected ({reason}); marked no_op")
            entries.append({"note_index": index, **_NO_OP_ENTRY})
            continue

        entries.append({"note_index": index, **clean})

    return entries, warnings


def compile_directives(
    entries: Iterable[dict],
    base_solar: list[float],
    base_reserve: float,
    capacity: float,
) -> dict[str, Any]:
    """Fold validated directives into the numbers the optimizer consumes (§5.3).

    This is the ONLY place a directive turns into a mathematical constraint.
    """
    effective_solar = list(base_solar)
    reserve = [base_reserve] * 24
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    grid_cap: dict[int, float] = {}

    for entry in entries:
        if not entry.get("applies"):
            continue
        directive_type = entry["directive_type"]
        adjustment = entry.get("structured_adjustment") or {}
        hours = adjustment.get("hours") or []

        if directive_type == "solar_reduction":
            factor = float(adjustment["factor"])
            for hour in hours:
                effective_solar[hour] = base_solar[hour] * factor

        elif directive_type == "minimum_battery_reserve":
            # Directive reserve overrides the base floor only where it is higher.
            value = float(adjustment["minimum_energy_kwh"])
            for hour in hours:
                reserve[hour] = max(reserve[hour], value)

        elif directive_type == "no_charge_window":
            no_charge.update(hours)

        elif directive_type == "no_discharge_window":
            no_discharge.update(hours)

        elif directive_type == "max_grid_window":
            value = float(adjustment["max_grid_kwh"])
            for hour in hours:
                grid_cap[hour] = min(grid_cap.get(hour, float("inf")), value)

    return {
        "effective_solar": effective_solar,
        "reserve": reserve,
        "no_charge": no_charge,
        "no_discharge": no_discharge,
        "grid_cap": grid_cap,
    }
