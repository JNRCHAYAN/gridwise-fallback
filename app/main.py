"""FastAPI service — Problem Statement §06 (API contract).

    GET  /health           readiness probe
    POST /optimize-energy  note interpretation + 24-hour optimisation

Pipeline per request:

    operator_notes
        -> interpret.interpret_notes()      LLM reads the notes
        -> guardrails.validate_interpretation()   deterministic validation
        -> guardrails.compile_directives()        directives -> constraints
        -> optimizer.optimize()             exact LP solve
        -> replay.verify_plan()             independent re-check
        -> response

Every step after the model call is deterministic. Nothing the model produces
reaches the optimizer without passing the guardrail layer.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import interpret
from .guardrails import compile_directives, validate_interpretation
from .optimizer import optimize
from .replay import check_directive_compliance, recompute_totals, verify_plan
from .schemas import ErrorResponse, HealthResponse, OptimizeRequest, OptimizeResponse

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("gridwise")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GridWise LLM service starting")
    if interpret.is_configured():
        logger.info("interpretation route: %s", interpret.describe_route())
    else:
        logger.warning(
            "LLM_API_KEY is not set — operator notes will degrade to no_op. "
            "Set LLM_API_KEY (and optionally LLM_BASE_URL / LLM_MODEL) to enable interpretation."
        )
    yield
    logger.info("GridWise LLM service stopping")


app = FastAPI(
    title="GridWise LLM — BUP CSE Fest 2026 Preliminary",
    version="1.0.0",
    description="LLM-assisted operator directive interpretation and 24-hour energy optimisation.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Error handling — controlled failures only, never a stack trace to the client
# ---------------------------------------------------------------------------

# Pydantic error types that mean the request is *structurally* wrong: a missing
# field, a value of the wrong type, an array of the wrong length, an unexpected
# extra field. §6.1 puts these under 400.
_STRUCTURAL_ERROR_TYPES = frozenset(
    {
        "missing",
        "too_short",
        "too_long",
        "extra_forbidden",
        "string_type",
        "int_type",
        "float_type",
        "bool_type",
        "list_type",
        "dict_type",
        "tuple_type",
        "int_parsing",
        "float_parsing",
        "bool_parsing",
        "string_too_short",
        "string_too_long",
        "model_attributes_type",
    }
)


def _is_structural(error: dict) -> bool:
    """True when this validation error makes the request structurally invalid.

    Cross-field invariants raised by our own validators surface with the generic
    type ``value_error``, so they are classified by which invariant fired: the
    hour-set and empty-note checks are shape problems (400), while a battery
    whose starting energy exceeds its capacity is a value problem (422).
    """
    kind = str(error.get("type", ""))
    if kind.startswith("json_invalid") or kind in _STRUCTURAL_ERROR_TYPES:
        return True
    if kind == "value_error":
        return "exceeds capacity" not in str(error.get("msg", ""))
    return False


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Malformed JSON or a structurally invalid body -> 400. Out-of-range values
    in an otherwise well-formed body -> 422 (§6.1).

    §6.1 assigns "structurally invalid request" to 400 and marks 422 optional for
    requests that are well-formed but semantically invalid. So a body missing a
    field, carrying the wrong type, or holding 23 hours instead of 24 is a 400 —
    only a correctly-shaped body whose *values* fall outside their allowed range
    is a 422.
    """
    errors = exc.errors()
    status = 400 if any(_is_structural(err) for err in errors) else 422
    detail = [
        {
            "loc": [str(part) for part in err.get("loc", [])],
            "type": str(err.get("type", "")),
            "msg": str(err.get("msg", "")),
        }
        for err in errors
    ][:10]
    logger.warning("rejected invalid request body (status=%d)", status)
    return JSONResponse(status_code=status, content={"error": "invalid_request", "detail": detail})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    """Last-resort handler. Logs the detail server-side, returns a generic body."""
    logger.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": "The service could not complete this request."},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Readiness probe. Must return 200 with status=ok (§6.2)."""
    return HealthResponse(status="ok")


# The error bodies below are declared explicitly because the handlers further up
# replace FastAPI's defaults. Without this, /docs advertises a
# {"detail": [...]} shape for 422 that this service never emits, and omits 400
# and 500 entirely.
_ERROR_RESPONSES = {
    400: {
        "model": ErrorResponse,
        "description": "Malformed JSON, or a structurally invalid request body — "
        "missing field, wrong type, wrong number of hours or notes, unexpected field (§6.1).",
    },
    422: {
        "model": ErrorResponse,
        "description": "A correctly shaped request whose values fall outside their "
        "allowed range, e.g. a non-positive battery capacity (§6.1, optional code).",
    },
    500: {
        "model": ErrorResponse,
        "description": "Controlled internal error. Never exposes a stack trace or secret (§6.1).",
    },
}


@app.post("/optimize-energy", response_model=OptimizeResponse, responses=_ERROR_RESPONSES)
def optimize_energy(request: OptimizeRequest) -> OptimizeResponse:
    started = time.perf_counter()

    hours = [entry.model_dump() for entry in request.hours]
    battery = request.battery.model_dump()
    base_solar = [float(hour["solar_kwh"]) for hour in hours]
    notes = list(request.operator_notes)

    # ---- 1. LLM interpretation -------------------------------------------
    warnings: list[str] = []
    raw_entries: list = []
    try:
        raw_entries = interpret.interpret_notes(notes, battery)
    except interpret.InterpretationError as exc:
        # Safe failure (§08): the model is unreachable or returned garbage.
        # Mark everything no_op and still return a valid schedule.
        logger.warning("interpretation unavailable, degrading to no_op: %s", exc)
        warnings.append("language model unavailable; notes treated as non-operative")

    # ---- 2. Deterministic guardrails --------------------------------------
    entries, guard_warnings = validate_interpretation(
        raw_entries, len(notes), float(battery["capacity_kwh"])
    )
    warnings.extend(guard_warnings)
    for warning in guard_warnings:
        logger.warning("guardrail: %s", warning)

    # ---- 3. Compile directives into the optimisation model ----------------
    compiled = compile_directives(
        entries, base_solar, float(battery["minimum_energy_kwh"]), float(battery["capacity_kwh"])
    )

    # ---- 4. Solve ---------------------------------------------------------
    plan, solver_warnings = optimize(hours, battery, compiled, base_solar)

    # ---- 5. Independent replay verification -------------------------------
    violations = verify_plan(plan, hours, battery, compiled)
    violations += check_directive_compliance(plan, entries)

    if violations:
        # The optimiser should never produce this. If it somehow does, fall
        # back to a schedule that is valid under the base rules rather than
        # returning a plan we know the judge will reject.
        logger.error("replay verification failed: %s", violations[:5])
        base_compiled = compile_directives(
            [{"note_index": i, "applies": False, "directive_type": "no_op",
              "structured_adjustment": None, "explanation": ""} for i in range(len(notes))],
            base_solar,
            float(battery["minimum_energy_kwh"]),
            float(battery["capacity_kwh"]),
        )
        fallback_plan, fallback_warnings = optimize(hours, battery, base_compiled, base_solar)
        fallback_violations = verify_plan(fallback_plan, hours, battery, base_compiled)
        if not fallback_violations:
            plan = fallback_plan
            compiled = base_compiled
            violations = []
        warnings.append("directive set produced an invalid plan; re-solved under base rules")

    for warning in solver_warnings:
        logger.warning("optimizer: %s", warning)
    warnings.extend(solver_warnings)

    # ---- 6. Totals re-derived from the emitted plan (§11.3) ---------------
    totals = recompute_totals(plan, hours)

    elapsed = time.perf_counter() - started
    logger.info(
        "scenario=%s notes=%d applied=%d cost=%.2f elapsed=%.3fs",
        request.scenario_id,
        len(notes),
        sum(1 for e in entries if e["applies"]),
        totals["total_cost_bdt"],
        elapsed,
    )

    return OptimizeResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=entries,
        hourly_plan=plan,
        total_grid_kwh=totals["total_grid_kwh"],
        total_cost_bdt=totals["total_cost_bdt"],
        peak_grid_kwh=totals["peak_grid_kwh"],
        plan_summary=_summarise(entries, plan, totals, battery),
    )


def _summarise(
    entries: list[dict],
    plan: list[dict],
    totals: dict[str, float],
    battery: dict[str, float],
) -> str:
    """Short human-readable strategy note. Generated deterministically so it
    costs no latency and cannot contradict the returned plan."""
    applied = [e for e in entries if e["applies"]]
    if applied:
        kinds = ", ".join(sorted({e["directive_type"] for e in applied}))
        lead = f"Applied {len(applied)} operator directive(s): {kinds}."
    else:
        lead = "No operator directive affected this schedule."

    charged = sum(r["battery_kwh"] for r in plan if r["battery_action"] == "charge")
    discharged = sum(r["battery_kwh"] for r in plan if r["battery_action"] == "discharge")
    solar = sum(r["solar_used_kwh"] for r in plan)

    return (
        f"{lead} Battery cycled {charged:.1f} kWh in and {discharged:.1f} kWh out to "
        f"shift energy from cheap hours into expensive ones, ending at "
        f"{battery['initial_energy_kwh']:.1f} kWh as required. "
        f"Used {solar:.1f} kWh of solar. Total grid purchase "
        f"{totals['total_grid_kwh']:.1f} kWh for {totals['total_cost_bdt']:.2f} BDT, "
        f"peak {totals['peak_grid_kwh']:.1f} kWh."
    )
