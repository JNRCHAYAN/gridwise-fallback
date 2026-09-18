"""Independent replay verification — Problem Statement §11.

The judge replays the returned schedule hour by hour against the real
constraints. This module does the same thing to our own output *before* it is
returned, so a numerically broken plan never leaves the service.

It deliberately re-derives everything from the emitted plan rather than
trusting the optimizer's internal state.
"""

from __future__ import annotations

import math
from typing import Any

TOLERANCE = 0.01  # §11.5 — absolute tolerance of 0.01 kWh / 0.01 BDT.


def recompute_totals(plan: list[dict[str, Any]], hours: list[dict[str, Any]]) -> dict[str, float]:
    """Derive every reported total from hourly_plan alone.

    §11.3 requires total_grid_kwh, total_cost_bdt and peak_grid_kwh to match
    values recalculated from hourly_plan. Computing them here — rather than
    from solver internals — makes that true by construction.
    """
    tariff = {int(h["hour"]): float(h["tariff_bdt_per_kwh"]) for h in hours}
    total_grid = 0.0
    total_cost = 0.0
    peak = 0.0
    for row in plan:
        hour = int(row["hour"])
        grid = float(row["grid_kwh"])
        total_grid += grid
        total_cost += grid * tariff.get(hour, 0.0)
        peak = max(peak, grid)
    return {
        "total_grid_kwh": round(total_grid, 6),
        "total_cost_bdt": round(total_cost, 6),
        "peak_grid_kwh": round(peak, 6),
    }


def verify_plan(
    plan: list[dict[str, Any]],
    hours: list[dict[str, Any]],
    battery: dict[str, float],
    compiled: dict[str, Any],
) -> list[str]:
    """Replay the plan against every rule. Returns a list of violations."""
    violations: list[str] = []
    by_hour = {int(h["hour"]): h for h in hours}
    effective_solar = [float(v) for v in compiled["effective_solar"]]
    reserve = [float(v) for v in compiled["reserve"]]
    no_charge: set[int] = compiled["no_charge"]
    no_discharge: set[int] = compiled["no_discharge"]
    grid_cap: dict[int, float] = compiled["grid_cap"]

    # ---- structural -------------------------------------------------------
    if len(plan) != 24:
        violations.append(f"hourly_plan has {len(plan)} entries, expected 24")
        return violations

    seen = [int(row["hour"]) for row in plan]
    if sorted(seen) != list(range(24)):
        violations.append("hourly_plan hours are not exactly 0..23")

    capacity = float(battery["capacity_kwh"])
    initial = float(battery["initial_energy_kwh"])
    max_charge = float(battery["max_charge_kwh_per_hour"])
    max_discharge = float(battery["max_discharge_kwh_per_hour"])

    energy = initial
    for row in plan:
        hour = int(row["hour"])
        source = by_hour.get(hour)
        if source is None:
            violations.append(f"hour {hour} missing from scenario")
            continue

        grid = float(row["grid_kwh"])
        solar_used = float(row["solar_used_kwh"])
        action = row["battery_action"]
        magnitude = float(row["battery_kwh"])

        # ---- §11.3 non-negative / finite ---------------------------------
        for name, value in (("grid_kwh", grid), ("solar_used_kwh", solar_used),
                            ("battery_kwh", magnitude)):
            if not math.isfinite(value):
                violations.append(f"hour {hour}: {name} is not finite")
            elif value < -TOLERANCE:
                violations.append(f"hour {hour}: {name} is negative ({value})")

        # ---- §10.3 action consistency ------------------------------------
        if action not in ("charge", "discharge", "idle"):
            violations.append(f"hour {hour}: invalid battery_action {action!r}")
        if action == "idle" and magnitude > TOLERANCE:
            violations.append(f"hour {hour}: idle action with battery_kwh={magnitude}")
        if action == "charge" and magnitude <= 0:
            violations.append(f"hour {hour}: charge action with battery_kwh={magnitude}")
        if action == "discharge" and magnitude <= 0:
            violations.append(f"hour {hour}: discharge action with battery_kwh={magnitude}")

        charge = magnitude if action == "charge" else 0.0
        discharge = magnitude if action == "discharge" else 0.0

        # ---- §9.5 energy balance -----------------------------------------
        supply = grid + solar_used + discharge
        required = float(source["demand_kwh"]) + charge
        if abs(supply - required) > TOLERANCE:
            violations.append(
                f"hour {hour}: energy balance {supply:.4f} != {required:.4f}"
            )

        # ---- §9.4 solar usage --------------------------------------------
        if solar_used > effective_solar[hour] + TOLERANCE:
            violations.append(
                f"hour {hour}: solar_used {solar_used:.4f} exceeds "
                f"effective solar {effective_solar[hour]:.4f}"
            )

        # ---- §9.3 rate limits --------------------------------------------
        if charge > max_charge + TOLERANCE:
            violations.append(
                f"hour {hour}: charge {charge:.4f} exceeds limit {max_charge:.4f}"
            )
        if discharge > max_discharge + TOLERANCE:
            violations.append(
                f"hour {hour}: discharge {discharge:.4f} exceeds limit {max_discharge:.4f}"
            )

        # ---- directive windows -------------------------------------------
        if action == "charge" and hour in no_charge:
            violations.append(f"hour {hour}: charged during a no_charge_window")
        if action == "discharge" and hour in no_discharge:
            violations.append(f"hour {hour}: discharged during a no_discharge_window")
        if hour in grid_cap and grid > grid_cap[hour] + TOLERANCE:
            violations.append(
                f"hour {hour}: grid {grid:.4f} exceeds max_grid_window {grid_cap[hour]:.4f}"
            )

        # ---- §9.1 battery state transition -------------------------------
        energy += charge - discharge
        reported = float(row["battery_energy_after_kwh"])
        if abs(energy - reported) > TOLERANCE:
            violations.append(
                f"hour {hour}: battery_energy_after {reported:.4f} but replay gives {energy:.4f}"
            )

        # ---- §9.2 battery bounds -----------------------------------------
        floor = max(reserve[hour], float(battery["minimum_energy_kwh"]))
        if energy < floor - TOLERANCE:
            violations.append(f"hour {hour}: battery {energy:.4f} below floor {floor:.4f}")
        if energy > capacity + TOLERANCE:
            violations.append(f"hour {hour}: battery {energy:.4f} above capacity {capacity:.4f}")

    # ---- §9.6 end-of-day neutrality --------------------------------------
    if abs(energy - initial) > TOLERANCE:
        violations.append(
            f"end-of-day battery {energy:.4f} does not equal initial {initial:.4f}"
        )

    return violations


def check_directive_compliance(
    plan: list[dict[str, Any]],
    entries: list[dict[str, Any]],
) -> list[str]:
    """Explicitly confirm each applicable directive is honoured by the plan.

    This is the §11.2 'downstream application' check the judge runs — separate
    from the correctness of the interpretation itself.
    """
    violations: list[str] = []
    rows = {int(r["hour"]): r for r in plan}

    for entry in entries:
        if not entry.get("applies"):
            continue
        adjustment = entry.get("structured_adjustment") or {}
        hours = adjustment.get("hours") or []
        dtype = entry["directive_type"]

        for hour in hours:
            row = rows.get(hour)
            if row is None:
                continue
            if dtype == "no_charge_window" and row["battery_action"] == "charge":
                violations.append(f"directive not applied: charged in no_charge hour {hour}")
            elif dtype == "no_discharge_window" and row["battery_action"] == "discharge":
                violations.append(f"directive not applied: discharged in no_discharge hour {hour}")
            elif dtype == "max_grid_window":
                if float(row["grid_kwh"]) > float(adjustment["max_grid_kwh"]) + TOLERANCE:
                    violations.append(
                        f"directive not applied: grid cap exceeded at hour {hour}"
                    )
            elif dtype == "minimum_battery_reserve":
                if float(row["battery_energy_after_kwh"]) < float(
                    adjustment["minimum_energy_kwh"]
                ) - TOLERANCE:
                    violations.append(
                        f"directive not applied: reserve breached at hour {hour}"
                    )

    return violations
