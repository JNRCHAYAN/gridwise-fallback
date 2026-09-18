"""Request and response schemas — the exact API contract from Problem Statement §07 and §10."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.examples import EXAMPLE_REQUEST

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

DIRECTIVE_TYPES: frozenset[str] = frozenset(
    {
        "solar_reduction",
        "minimum_battery_reserve",
        "no_charge_window",
        "no_discharge_window",
        "max_grid_window",
        "no_op",
    }
)

# Directive types that carry a structured_adjustment body.
APPLICABLE_DIRECTIVES: frozenset[str] = DIRECTIVE_TYPES - {"no_op"}


class HourEntry(BaseModel):
    """One hour of the supplied 24-hour scenario."""

    model_config = ConfigDict(extra="forbid")

    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class Battery(BaseModel):
    """Battery physical parameters."""

    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_consistency(self) -> "Battery":
        if self.initial_energy_kwh > self.capacity_kwh + 1e-9:
            raise ValueError("initial_energy_kwh exceeds capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh + 1e-9:
            raise ValueError("minimum_energy_kwh exceeds capacity_kwh")
        return self


class HealthResponse(BaseModel):
    """GET /health response (§6.2)."""

    model_config = ConfigDict(json_schema_extra={"example": {"status": "ok"}})

    status: Literal["ok"] = "ok"


class ErrorDetail(BaseModel):
    """One validation problem, mirroring the handler in app/main.py."""

    loc: list[str] = Field(default_factory=list)
    type: str = ""
    msg: str = ""


class ErrorResponse(BaseModel):
    """The body returned for 400, 422 and 500 (§6.1).

    Declared explicitly so the published contract matches what the custom
    exception handler in app/main.py actually emits. Without it FastAPI
    documents its own default ``{"detail": [...]}`` shape, which this service
    never returns.
    """

    error: str
    detail: list[ErrorDetail] | str


class OptimizeRequest(BaseModel):
    """POST /optimize-energy request body."""

    # The example is what Swagger UI pre-fills into "Try it out". Without it the
    # generated body has one hour entry and a zero battery capacity, so the very
    # first Execute on /docs fails validation before the user has changed a thing.
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": EXAMPLE_REQUEST})

    scenario_id: str = Field(min_length=1)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourEntry] = Field(min_length=24, max_length=24)
    battery: Battery

    @model_validator(mode="after")
    def _check_shape(self) -> "OptimizeRequest":
        seen = sorted(h.hour for h in self.hours)
        if seen != list(range(24)):
            raise ValueError("hours must contain exactly one entry for each hour 0..23")
        for note in self.operator_notes:
            if not note or not note.strip():
                raise ValueError("operator_notes entries must be non-empty strings")
        return self


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------


class DirectiveInterpretation(BaseModel):
    """One machine-checkable interpretation entry per operator note (§10.2)."""

    note_index: int = Field(ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[dict[str, Any]] = None
    explanation: str


class HourlyPlanEntry(BaseModel):
    """One hour of the returned schedule (§10.3)."""

    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)


_RESPONSE_EXAMPLE: dict[str, Any] = {
    "scenario_id": "SAMPLE-01",
    "directive_interpretation": [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
            "explanation": "Solar reduced to 25% during panel cleaning noon to 2 PM",
        },
        {
            "note_index": 1,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "Sports registration deadline is unrelated to the energy schedule",
        },
    ],
    # Abridged for readability — the live response always carries all 24 hours.
    "hourly_plan": [
        {
            "hour": 0,
            "grid_kwh": 40.0,
            "solar_used_kwh": 0.0,
            "battery_action": "discharge",
            "battery_kwh": 50.0,
            "battery_energy_after_kwh": 60.0,
        },
        {
            "hour": 1,
            "grid_kwh": 65.0,
            "solar_used_kwh": 0.0,
            "battery_action": "discharge",
            "battery_kwh": 20.0,
            "battery_energy_after_kwh": 40.0,
        },
    ],
    "total_grid_kwh": 2692.5,
    "total_cost_bdt": 38365.0,
    "peak_grid_kwh": 187.5,
    "plan_summary": "Applied 1 operator directive(s): solar_reduction.",
}


class OptimizeResponse(BaseModel):
    """POST /optimize-energy response body (§10.1)."""

    model_config = ConfigDict(json_schema_extra={"example": _RESPONSE_EXAMPLE})

    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
