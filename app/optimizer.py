"""Linear-programming optimizer — Problem Statement §05.2, §09.

The entire scheduling problem is linear, so it is solved exactly rather than
heuristically. Solving to true optimality makes the judge's cost ratio
``organizer_optimal / our_cost`` equal to 1.0 on every case.

Variables, for each hour h in 0..23:
    grid[h]       kWh bought from the grid          >= 0
    solar[h]      kWh of solar actually used        0 <= s <= effective_solar[h]
    charge[h]     kWh into the battery              <= max_charge_kwh_per_hour
    discharge[h]  kWh out of the battery            <= max_discharge_kwh_per_hour

Objective:  minimise  SUM( grid[h] * tariff[h] )

Subject to:
    energy balance   grid + solar + discharge - charge = demand          (24 eq)
    battery bounds   reserve[h] <= E0 + SUM_{i<=h}(charge[i]-discharge[i]) <= capacity
    neutrality       SUM(charge) - SUM(discharge) = 0                    (1 eq)
    plus every window directive from the guardrail layer.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.optimize import linprog

HOURS = 24
_VARS_PER_HOUR = 4
_GRID, _SOLAR, _CHARGE, _DISCHARGE = 0, 1, 2, 3

# Anything below this is solver noise rather than a real instruction.
EPS = 1e-7

SOLVER_FAILURE_MESSAGE = (
    "Optimizer could not satisfy every operator directive simultaneously; "
    "fell back to a schedule valid under the base GridWise rules."
)


def _index(hour: int, kind: int) -> int:
    return hour * _VARS_PER_HOUR + kind


def _build_and_solve(
    demand: list[float],
    tariff: list[float],
    battery: dict[str, float],
    effective_solar: list[float],
    reserve: list[float],
    no_charge: set[int],
    no_discharge: set[int],
    grid_cap: dict[int, float],
    use_directives: bool,
):
    """Assemble the LP and hand it to HiGHS. Returns the scipy result object."""
    n_vars = HOURS * _VARS_PER_HOUR
    objective = np.zeros(n_vars)
    for hour in range(HOURS):
        objective[_index(hour, _GRID)] = tariff[hour]

    # ---- variable bounds --------------------------------------------------
    bounds: list[tuple[float, float | None]] = [(0.0, None)] * n_vars
    for hour in range(HOURS):
        bounds[_index(hour, _SOLAR)] = (0.0, max(0.0, effective_solar[hour]))

        charge_cap = battery["max_charge_kwh_per_hour"]
        if use_directives and hour in no_charge:
            charge_cap = 0.0
        bounds[_index(hour, _CHARGE)] = (0.0, charge_cap)

        discharge_cap = battery["max_discharge_kwh_per_hour"]
        if use_directives and hour in no_discharge:
            discharge_cap = 0.0
        bounds[_index(hour, _DISCHARGE)] = (0.0, discharge_cap)

        grid_upper = None
        if use_directives and hour in grid_cap:
            grid_upper = grid_cap[hour]
        bounds[_index(hour, _GRID)] = (0.0, grid_upper)

    # ---- energy balance (equality) ----------------------------------------
    a_eq = np.zeros((HOURS, n_vars))
    b_eq = np.zeros(HOURS)
    for hour in range(HOURS):
        a_eq[hour, _index(hour, _GRID)] = 1.0
        a_eq[hour, _index(hour, _SOLAR)] = 1.0
        a_eq[hour, _index(hour, _DISCHARGE)] = 1.0
        a_eq[hour, _index(hour, _CHARGE)] = -1.0
        b_eq[hour] = demand[hour]

    # ---- battery level bounds (inequality) --------------------------------
    a_ub: list[np.ndarray] = []
    b_ub: list[float] = []
    initial = battery["initial_energy_kwh"]
    capacity = battery["capacity_kwh"]

    for hour in range(HOURS):
        net = np.zeros(n_vars)
        for earlier in range(hour + 1):
            net[_index(earlier, _CHARGE)] = 1.0
            net[_index(earlier, _DISCHARGE)] = -1.0

        # E0 + net·x <= capacity
        a_ub.append(net.copy())
        b_ub.append(capacity - initial)

        # E0 + net·x >= floor  ->  -net·x <= E0 - floor
        # A directive reserve may only raise the base floor, never lower it (§5.3).
        floor = max(reserve[hour], battery["minimum_energy_kwh"])
        a_ub.append(-net)
        b_ub.append(initial - floor)

    # ---- end-of-day neutrality (equality) ---------------------------------
    neutrality = np.zeros(n_vars)
    for hour in range(HOURS):
        neutrality[_index(hour, _CHARGE)] = 1.0
        neutrality[_index(hour, _DISCHARGE)] = -1.0
    a_eq = np.vstack([a_eq, neutrality])
    b_eq = np.append(b_eq, 0.0)

    return linprog(
        objective,
        A_ub=np.array(a_ub),
        b_ub=np.array(b_ub),
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )


def _clean_solution(
    solution: np.ndarray,
    demand: list[float],
    effective_solar: list[float],
    initial: float,
) -> list[dict[str, Any]]:
    """Turn raw solver output into schema-valid, physically exact plan rows."""
    grid = [max(0.0, float(solution[_index(h, _GRID)])) for h in range(HOURS)]
    solar = [max(0.0, float(solution[_index(h, _SOLAR)])) for h in range(HOURS)]
    charge = [max(0.0, float(solution[_index(h, _CHARGE)])) for h in range(HOURS)]
    discharge = [max(0.0, float(solution[_index(h, _DISCHARGE)])) for h in range(HOURS)]

    # Solar can never exceed what was available (guards against solver noise).
    for h in range(HOURS):
        if solar[h] > effective_solar[h]:
            solar[h] = max(0.0, effective_solar[h])

    # Charging and discharging at the same instant is physically pointless and
    # only ever appears as solver noise. Net it away; E is unchanged by doing so.
    for h in range(HOURS):
        overlap = min(charge[h], discharge[h])
        if overlap > 0.0:
            charge[h] -= overlap
            discharge[h] -= overlap

    # Snap negligible magnitudes to zero so battery_action is unambiguous.
    for h in range(HOURS):
        if charge[h] < EPS:
            charge[h] = 0.0
        if discharge[h] < EPS:
            discharge[h] = 0.0
        if solar[h] < EPS:
            solar[h] = 0.0
        if grid[h] < EPS:
            grid[h] = 0.0

    plan: list[dict[str, Any]] = []
    energy = initial
    for h in range(HOURS):
        if charge[h] > 0.0:
            action, magnitude = "charge", charge[h]
            energy += magnitude
        elif discharge[h] > 0.0:
            action, magnitude = "discharge", discharge[h]
            energy -= magnitude
        else:
            action, magnitude = "idle", 0.0

        # Re-derive grid from the balance equation so the emitted plan is
        # internally exact rather than merely close.
        balanced_grid = demand[h] + charge[h] - discharge[h] - solar[h]
        if balanced_grid < 0.0 and balanced_grid > -1e-6:
            balanced_grid = 0.0
        if abs(balanced_grid - grid[h]) > 1e-9:
            grid[h] = max(0.0, balanced_grid)

        plan.append(
            {
                "hour": h,
                "grid_kwh": round(grid[h], 6),
                "solar_used_kwh": round(solar[h], 6),
                "battery_action": action,
                "battery_kwh": round(magnitude, 6),
                "battery_energy_after_kwh": round(energy, 6),
            }
        )
    return plan


def _idle_plan(
    demand: list[float],
    effective_solar: list[float],
    initial: float,
) -> list[dict[str, Any]]:
    """Last-resort plan: battery untouched, no directive can be violated by
    charging or discharging. Always satisfies energy balance and neutrality."""
    plan: list[dict[str, Any]] = []
    for h in range(HOURS):
        solar_used = min(demand[h], max(0.0, effective_solar[h]))
        grid = max(0.0, demand[h] - solar_used)
        plan.append(
            {
                "hour": h,
                "grid_kwh": round(grid, 6),
                "solar_used_kwh": round(solar_used, 6),
                "battery_action": "idle",
                "battery_kwh": 0.0,
                "battery_energy_after_kwh": round(initial, 6),
            }
        )
    return plan


def optimize(
    hours: list[dict[str, Any]],
    battery: dict[str, float],
    compiled: dict[str, Any],
    base_solar: list[float],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Produce the cheapest valid 24-hour schedule.

    Returns ``(plan, warnings)``. The plan is always 24 rows long and always
    satisfies energy balance, battery bounds, rate limits and neutrality.
    """
    demand = [float(h["demand_kwh"]) for h in hours]
    tariff = [float(h["tariff_bdt_per_kwh"]) for h in hours]
    effective_solar = [float(v) for v in compiled["effective_solar"]]

    warnings: list[str] = []

    # Attempt 1 — the real problem, every validated directive applied.
    result = _build_and_solve(
        demand,
        tariff,
        battery,
        effective_solar,
        compiled["reserve"],
        compiled["no_charge"],
        compiled["no_discharge"],
        compiled["grid_cap"],
        use_directives=True,
    )
    if result.status == 0 and result.x is not None:
        return _clean_solution(result.x, demand, effective_solar, battery["initial_energy_kwh"]), warnings

    # Attempt 2 — directives jointly infeasible. The organiser guarantees this
    # will not happen on a scoring case, but a malformed or contradictory
    # interpretation must never take the service down. Drops every directive,
    # including any solar reduction, and re-solves against the base scenario.
    warnings.append(SOLVER_FAILURE_MESSAGE)
    result = _build_and_solve(
        demand,
        tariff,
        battery,
        [float(v) for v in base_solar],
        [battery["minimum_energy_kwh"]] * HOURS,
        set(),
        set(),
        {},
        use_directives=False,
    )
    if result.status == 0 and result.x is not None:
        return _clean_solution(result.x, demand, base_solar, battery["initial_energy_kwh"]), warnings

    # Attempt 3 — even the base problem failed. Return a plan that is valid by
    # construction.
    warnings.append("Base optimization infeasible; returned an idle-battery schedule.")
    return _idle_plan(demand, base_solar, battery["initial_energy_kwh"]), warnings
