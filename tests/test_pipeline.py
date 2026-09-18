"""Test suite for the GridWise LLM pipeline.

    pytest -q

Covers the deterministic half of the pipeline exhaustively (guardrails,
optimizer, replay) and the API contract. The LLM call itself is stubbed, so
the suite runs offline and never depends on a provider being reachable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.guardrails import compile_directives, normalize_hours, validate_interpretation
from app.optimizer import optimize
from app.replay import check_directive_compliance, recompute_totals, verify_plan

# A copy of the pack ships in tools/ so the suite runs from the repository alone
# (§02). The official file beside the project wins when it is present, since that
# is the copy the organisers distributed during the round.
_VENDORED_PACK = ROOT / "tools" / "public_sample_cases.json"
_EXTERNAL_PACK = ROOT.parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
PACK = _EXTERNAL_PACK if _EXTERNAL_PACK.exists() else _VENDORED_PACK


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def public_cases() -> list[dict]:
    if not PACK.exists():
        pytest.skip(f"public sample pack not found at {PACK}")
    with PACK.open(encoding="utf-8") as handle:
        return json.load(handle)["cases"]


def _battery(case: dict) -> dict:
    return {k: float(v) for k, v in case["input"]["battery"].items()}


def _solve(case: dict, raw_entries):
    """Run guardrails -> optimizer -> replay for one case."""
    request = case["input"]
    battery = _battery(case)
    base_solar = [float(h["solar_kwh"]) for h in request["hours"]]

    entries, warnings = validate_interpretation(
        raw_entries, len(request["operator_notes"]), battery["capacity_kwh"]
    )
    compiled = compile_directives(
        entries, base_solar, battery["minimum_energy_kwh"], battery["capacity_kwh"]
    )
    plan, _ = optimize(request["hours"], battery, compiled, base_solar)
    return entries, compiled, plan, warnings, battery, request["hours"]


# ---------------------------------------------------------------------------
# Guardrails — hours normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ([13, 14], [13, 14]),
        ([14, 13], [13, 14]),          # unsorted input is sorted
        ([13, 13, 14], [13, 14]),      # duplicates removed
        ([13.0, 14.0], [13, 14]),      # whole floats accepted
        ([0, 23], [0, 23]),
        ([], []),
    ],
)
def test_normalize_hours_accepts(raw, expected):
    assert normalize_hours(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        [-1], [24], [25], [13.5], ["13"], [None], "13,14", [True], [13, "a"],
    ],
)
def test_normalize_hours_rejects(raw):
    assert normalize_hours(raw) is None


# ---------------------------------------------------------------------------
# Guardrails — entry validation
# ---------------------------------------------------------------------------


def test_no_op_entry_is_normalised():
    entries, _ = validate_interpretation(
        [{"note_index": 0, "applies": False, "directive_type": "no_op",
          "structured_adjustment": None, "explanation": "irrelevant"}],
        1,
        200.0,
    )
    assert entries[0]["directive_type"] == "no_op"
    assert entries[0]["applies"] is False
    assert entries[0]["structured_adjustment"] is None


def test_unsupported_directive_type_degrades_to_no_op():
    entries, warnings = validate_interpretation(
        [{"note_index": 0, "applies": True, "directive_type": "turn_off_sun",
          "structured_adjustment": {"hours": [1]}, "explanation": "bogus"}],
        1,
        200.0,
    )
    assert entries[0]["directive_type"] == "no_op"
    assert entries[0]["applies"] is False
    assert any("unsupported directive_type" in w for w in warnings)


def test_applicable_directive_with_applies_false_is_rejected():
    entries, warnings = validate_interpretation(
        [{"note_index": 0, "applies": False, "directive_type": "solar_reduction",
          "structured_adjustment": {"hours": [10], "factor": 0.5}, "explanation": "x"}],
        1,
        200.0,
    )
    assert entries[0]["directive_type"] == "no_op"
    assert any("applies=false" in w for w in warnings)


@pytest.mark.parametrize("factor", [-0.1, 1.1, 2.0, -1.0])
def test_solar_factor_outside_unit_interval_is_rejected(factor):
    entries, warnings = validate_interpretation(
        [{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
          "structured_adjustment": {"hours": [10], "factor": factor}, "explanation": "x"}],
        1,
        200.0,
    )
    assert entries[0]["directive_type"] == "no_op"
    assert any("outside [0, 1]" in w for w in warnings)


def test_reserve_above_capacity_is_rejected():
    entries, warnings = validate_interpretation(
        [{"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
          "structured_adjustment": {"hours": [18], "minimum_energy_kwh": 500.0},
          "explanation": "x"}],
        1,
        200.0,
    )
    assert entries[0]["directive_type"] == "no_op"
    assert any("exceeds capacity" in w for w in warnings)


def test_non_finite_numeric_value_is_rejected():
    entries, _ = validate_interpretation(
        [{"note_index": 0, "applies": True, "directive_type": "max_grid_window",
          "structured_adjustment": {"hours": [18], "max_grid_kwh": float("nan")},
          "explanation": "x"}],
        1,
        200.0,
    )
    assert entries[0]["directive_type"] == "no_op"


def test_one_entry_per_note_in_order_even_when_model_omits_or_duplicates():
    raw = [
        {"note_index": 2, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [5]}, "explanation": "third"},
        {"note_index": 2, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [6]}, "explanation": "duplicate"},
    ]
    entries, warnings = validate_interpretation(raw, 3, 200.0)
    assert [e["note_index"] for e in entries] == [0, 1, 2]
    assert entries[0]["directive_type"] == "no_op"
    assert entries[1]["directive_type"] == "no_op"
    assert entries[2]["directive_type"] == "no_charge_window"
    assert any("duplicate" in w for w in warnings)
    assert sum("no interpretation" in w for w in warnings) == 2


def test_out_of_range_note_index_is_dropped():
    entries, warnings = validate_interpretation(
        [{"note_index": 7, "applies": True, "directive_type": "no_charge_window",
          "structured_adjustment": {"hours": [5]}, "explanation": "x"}],
        1,
        200.0,
    )
    assert entries[0]["directive_type"] == "no_op"
    assert any("out-of-range" in w for w in warnings)


def test_non_list_model_output_marks_everything_no_op():
    entries, warnings = validate_interpretation("not a list", 2, 200.0)
    assert [e["directive_type"] for e in entries] == ["no_op", "no_op"]
    assert any("was not a list" in w for w in warnings)


def test_missing_note_index_falls_back_to_array_position():
    """A model that omits note_index must not forfeit every note."""
    raw = [
        {"applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [5]}, "explanation": "first"},
        {"applies": False, "directive_type": "no_op",
         "structured_adjustment": None, "explanation": "second"},
    ]
    entries, warnings = validate_interpretation(raw, 2, 200.0)
    assert entries[0]["directive_type"] == "no_charge_window"
    assert entries[1]["directive_type"] == "no_op"
    assert any("used array position" in w for w in warnings)


def test_whole_float_note_index_is_accepted():
    entries, _ = validate_interpretation(
        [{"note_index": 0.0, "applies": True, "directive_type": "no_discharge_window",
          "structured_adjustment": {"hours": [17]}, "explanation": "x"}],
        1,
        200.0,
    )
    assert entries[0]["directive_type"] == "no_discharge_window"


# ---------------------------------------------------------------------------
# Directive compilation — §5.3
# ---------------------------------------------------------------------------


def test_solar_reduction_multiplies_base_solar():
    base = [100.0] * 24
    entries = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
                "explanation": "x"}]
    compiled = compile_directives(entries, base, 40.0, 200.0)
    assert compiled["effective_solar"][12] == pytest.approx(25.0)
    assert compiled["effective_solar"][13] == pytest.approx(25.0)
    assert compiled["effective_solar"][11] == pytest.approx(100.0)


def test_reserve_takes_max_of_base_and_directive():
    base = [0.0] * 24
    entries = [{"note_index": 0, "applies": True,
                "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": [18], "minimum_energy_kwh": 60.0},
                "explanation": "x"}]
    compiled = compile_directives(entries, base, 100.0, 200.0)
    # Base floor is higher, so it wins.
    assert compiled["reserve"][18] == pytest.approx(100.0)

    entries[0]["structured_adjustment"]["minimum_energy_kwh"] = 150.0
    compiled = compile_directives(entries, base, 100.0, 200.0)
    assert compiled["reserve"][18] == pytest.approx(150.0)


def test_no_op_entry_changes_nothing():
    base = [50.0] * 24
    entries = [{"note_index": 0, "applies": False, "directive_type": "no_op",
                "structured_adjustment": None, "explanation": "x"}]
    compiled = compile_directives(entries, base, 40.0, 200.0)
    assert compiled["effective_solar"] == base
    assert compiled["reserve"] == [40.0] * 24
    assert not compiled["no_charge"] and not compiled["no_discharge"]
    assert not compiled["grid_cap"]


# ---------------------------------------------------------------------------
# Optimizer + replay against the public pack
# ---------------------------------------------------------------------------


def test_public_cases_are_optimal_and_valid(public_cases):
    """The ground-truth directives must yield a schedule that is valid and no
    more expensive than the organiser's own reference schedule."""
    for case in public_cases:
        expected = case["expected_output"]
        entries, compiled, plan, _, battery, hours = _solve(
            case, expected["directive_interpretation"]
        )

        violations = verify_plan(plan, hours, battery, compiled)
        violations += check_directive_compliance(plan, entries)
        assert not violations, f"{case['id']}: {violations}"

        totals = recompute_totals(plan, hours)
        reference = float(expected["total_cost_bdt"])
        assert totals["total_cost_bdt"] <= reference + 0.01, (
            f"{case['id']}: cost {totals['total_cost_bdt']} worse than reference {reference}"
        )


def test_reported_totals_match_hourly_plan(public_cases):
    """§11.3 — reported totals must equal values recalculated from hourly_plan.

    This is a self-consistency requirement, NOT byte-for-byte equality with the
    organiser's reference plan. §11.4 states that equivalent valid optimal
    schedules are accepted and that judging uses the *recalculated* cost.

    peak_grid_kwh is genuinely degenerate: more than one schedule can be
    cost-optimal while peaking at different hours. SAMPLE-01 is that case — our
    plan and the reference both cost 38365.00 and buy 2692.50 kWh, but peak at
    187.5 and 175.0 respectively, because the LP has several optima of equal
    cost and the objective does not price peak. Cost and total grid purchase
    *are* pinned by optimality, so those are the values compared to the
    reference.
    """
    for case in public_cases:
        expected = case["expected_output"]
        _, _, plan, _, _, hours = _solve(case, expected["directive_interpretation"])
        totals = recompute_totals(plan, hours)

        # ---- totals must agree with the plan they describe -----------------
        assert totals["total_grid_kwh"] == pytest.approx(
            sum(r["grid_kwh"] for r in plan), abs=0.01
        ), case["id"]
        assert totals["total_cost_bdt"] == pytest.approx(
            sum(r["grid_kwh"] * h["tariff_bdt_per_kwh"] for r, h in zip(plan, hours)),
            abs=0.01,
        ), case["id"]
        assert totals["peak_grid_kwh"] == pytest.approx(
            max(r["grid_kwh"] for r in plan), abs=0.01
        ), case["id"]

        # ---- pinned by optimality, so these must match the reference -------
        assert totals["total_cost_bdt"] == pytest.approx(
            float(expected["total_cost_bdt"]), abs=0.01
        ), case["id"]
        # total grid = total demand - solar used, and an optimum never curtails
        # solar while a positive tariff applies; every hour of the public pack
        # has a positive tariff, so this is pinned here.
        assert totals["total_grid_kwh"] == pytest.approx(
            float(expected["total_grid_kwh"]), abs=0.01
        ), case["id"]


def test_battery_returns_to_initial_energy(public_cases):
    for case in public_cases:
        expected = case["expected_output"]
        _, _, plan, _, battery, _ = _solve(case, expected["directive_interpretation"])
        assert plan[-1]["battery_energy_after_kwh"] == pytest.approx(
            battery["initial_energy_kwh"], abs=0.01
        ), case["id"]


def test_directives_are_actually_applied(public_cases):
    """Extraction without downstream application must not happen (§11.2)."""
    for case in public_cases:
        expected = case["expected_output"]
        entries, _, plan, _, _, _ = _solve(case, expected["directive_interpretation"])
        assert not check_directive_compliance(plan, entries), case["id"]


# ---------------------------------------------------------------------------
# Replay must actually catch violations
# ---------------------------------------------------------------------------


def test_replay_detects_broken_energy_balance(public_cases):
    case = public_cases[0]
    expected = case["expected_output"]
    _, compiled, plan, _, battery, hours = _solve(case, expected["directive_interpretation"])
    plan[3]["grid_kwh"] += 25.0
    assert any("energy balance" in v for v in verify_plan(plan, hours, battery, compiled))


def test_replay_detects_reserve_breach(public_cases):
    case = public_cases[0]
    expected = case["expected_output"]
    _, compiled, plan, _, battery, hours = _solve(case, expected["directive_interpretation"])
    compiled["reserve"][5] = battery["capacity_kwh"] + 50.0
    assert any("below floor" in v for v in verify_plan(plan, hours, battery, compiled))


def test_replay_detects_reported_battery_level_mismatch(public_cases):
    """A plan that *reports* a battery level its own transitions do not produce
    is rejected at the hour it happens (the running replay check), before the
    end-of-day check is even reached."""
    case = public_cases[0]
    expected = case["expected_output"]
    _, compiled, plan, _, battery, hours = _solve(case, expected["directive_interpretation"])
    plan[-1]["battery_energy_after_kwh"] += 10.0
    assert any("battery_energy_after" in v for v in verify_plan(plan, hours, battery, compiled))


def test_replay_detects_end_of_day_drift(public_cases):
    """End-of-day neutrality is a property of the battery *transitions*, not of
    the reported energy field: the day ends level only if every kWh drawn was
    put back. Break neutrality by cancelling one discharge and buying that
    energy from the grid instead, so energy balance still holds and the drift
    is the sole defect under test."""
    case = public_cases[0]
    expected = case["expected_output"]
    _, compiled, plan, _, battery, hours = _solve(case, expected["directive_interpretation"])

    row = next(r for r in plan if r["battery_action"] == "discharge")
    row["grid_kwh"] += row["battery_kwh"]  # replace the discharged energy with grid
    row["battery_action"] = "idle"
    row["battery_kwh"] = 0.0

    violations = verify_plan(plan, hours, battery, compiled)
    assert any("end-of-day" in v for v in violations), violations


def test_replay_detects_solar_overuse(public_cases):
    case = public_cases[0]
    expected = case["expected_output"]
    _, compiled, plan, _, battery, hours = _solve(case, expected["directive_interpretation"])
    plan[10]["solar_used_kwh"] = compiled["effective_solar"][10] + 20.0
    assert any("effective solar" in v for v in verify_plan(plan, hours, battery, compiled))


def test_replay_detects_grid_cap_violation(public_cases):
    case = next(c for c in public_cases if c["id"] == "SAMPLE-05")
    expected = case["expected_output"]
    _, compiled, plan, _, battery, hours = _solve(case, expected["directive_interpretation"])
    plan[18]["grid_kwh"] += 60.0
    assert any("max_grid_window" in v for v in verify_plan(plan, hours, battery, compiled))


# ---------------------------------------------------------------------------
# Optimizer fallback behaviour
# ---------------------------------------------------------------------------


def test_contradictory_directives_do_not_crash(public_cases):
    """Directives that cannot jointly hold must degrade, never raise."""
    case = public_cases[0]
    request = case["input"]
    battery = _battery(case)
    base_solar = [float(h["solar_kwh"]) for h in request["hours"]]

    # Demand every hour be met with zero grid and no discharge: impossible.
    entries = [
        {"note_index": 0, "applies": True, "directive_type": "max_grid_window",
         "structured_adjustment": {"hours": list(range(24)), "max_grid_kwh": 0.0},
         "explanation": "impossible"},
        {"note_index": 1, "applies": True, "directive_type": "no_discharge_window",
         "structured_adjustment": {"hours": list(range(24))}, "explanation": "impossible"},
    ]
    compiled = compile_directives(entries, base_solar, battery["minimum_energy_kwh"],
                                  battery["capacity_kwh"])
    plan, warnings = optimize(request["hours"], battery, compiled, base_solar)

    assert len(plan) == 24
    assert warnings, "expected a fallback warning"
    # The fallback plan must be valid under the base rules.
    base_compiled = compile_directives([], base_solar, battery["minimum_energy_kwh"],
                                       battery["capacity_kwh"])
    assert not verify_plan(plan, request["hours"], battery, base_compiled)


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(public_cases, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_module

    # Stub the model so API tests never touch the network.
    def fake_interpret(notes, battery, client=None):
        return [
            {"note_index": index, "applies": False, "directive_type": "no_op",
             "structured_adjustment": None, "explanation": "stubbed"}
            for index in range(len(notes))
        ]

    monkeypatch.setattr(main_module.interpret, "interpret_notes", fake_interpret)
    return TestClient(main_module.app)


def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_documents_a_request_that_actually_works(client):
    """Swagger UI pre-fills "Try it out" from the example in the OpenAPI schema.

    With no example declared, the generated body holds a single hour entry and a
    zero battery capacity — both of which the contract rejects — so the first
    Execute on /docs returns 422 before the user has typed anything. The example
    is therefore load-bearing: it must validate *and* be accepted by the live
    endpoint, not merely look plausible.
    """
    from app.schemas import OptimizeRequest

    spec = client.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["OptimizeRequest"]
    example = schema.get("example")
    assert example is not None, "OptimizeRequest carries no example for /docs to pre-fill"

    parsed = OptimizeRequest.model_validate(example)
    assert len(parsed.hours) == 24

    response = client.post("/optimize-energy", json=example)
    assert response.status_code == 200, response.text


def test_optimize_returns_full_schema(client, public_cases):
    case = public_cases[0]
    response = client.post("/optimize-energy", json=case["input"])
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["scenario_id"] == case["input"]["scenario_id"]
    assert len(body["hourly_plan"]) == 24
    assert len(body["directive_interpretation"]) == len(case["input"]["operator_notes"])
    assert isinstance(body["plan_summary"], str) and body["plan_summary"]
    for key in ("total_grid_kwh", "total_cost_bdt", "peak_grid_kwh"):
        assert isinstance(body[key], (int, float))


def test_malformed_json_returns_400(client):
    response = client.post(
        "/optimize-energy",
        content="{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_wrong_hour_count_returns_400(client, public_cases):
    """23 hours is a *structural* violation, and §6.1 assigns structural
    problems to 400 — 422 is reserved for well-formed bodies with bad values."""
    payload = json.loads(json.dumps(public_cases[0]["input"]))
    payload["hours"] = payload["hours"][:10]
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_too_many_notes_returns_400(client, public_cases):
    payload = json.loads(json.dumps(public_cases[0]["input"]))
    payload["operator_notes"] = ["a", "b", "c", "d"]
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_missing_field_returns_400(client, public_cases):
    payload = json.loads(json.dumps(public_cases[0]["input"]))
    del payload["battery"]
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_empty_note_returns_400(client, public_cases):
    payload = json.loads(json.dumps(public_cases[0]["input"]))
    payload["operator_notes"] = ["   "]
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_out_of_range_battery_value_returns_422(client, public_cases):
    """The body is correctly shaped — right fields, right types, 24 hours — but
    a value sits outside its allowed range. That is the 422 case in §6.1."""
    payload = json.loads(json.dumps(public_cases[0]["input"]))
    payload["battery"]["capacity_kwh"] = 0
    assert client.post("/optimize-energy", json=payload).status_code == 422


def test_inconsistent_battery_returns_422(client, public_cases):
    payload = json.loads(json.dumps(public_cases[0]["input"]))
    payload["battery"]["initial_energy_kwh"] = payload["battery"]["capacity_kwh"] + 50
    assert client.post("/optimize-energy", json=payload).status_code == 422


def test_openapi_error_contract_matches_what_the_handler_returns(client, public_cases):
    """The custom exception handlers replace FastAPI's defaults, so the schema
    FastAPI would generate on its own no longer describes this service. /docs
    must advertise the real error shape and the real status codes, otherwise the
    published contract contradicts the running behaviour."""
    spec = client.get("/openapi.json").json()
    post = spec["paths"]["/optimize-energy"]["post"]["responses"]

    for code in ("400", "422", "500"):
        assert code in post, f"{code} is undocumented"
        schema_ref = post[code]["content"]["application/json"]["schema"]
        assert schema_ref["$ref"].endswith("/ErrorResponse"), (
            f"{code} advertises {schema_ref}, not our ErrorResponse"
        )

    # And the documented shape is the shape an invalid request really produces.
    payload = json.loads(json.dumps(public_cases[0]["input"]))
    payload["hours"] = payload["hours"][:5]
    body = client.post("/optimize-energy", json=payload).json()
    assert set(body) == {"error", "detail"}
    assert body["error"] == "invalid_request"


def test_openapi_documents_the_health_response(client):
    spec = client.get("/openapi.json").json()
    health = spec["paths"]["/health"]["get"]
    assert health["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/HealthResponse"
    )


def test_model_failure_still_returns_valid_plan(client, public_cases, monkeypatch):
    """A dead provider must not break the service (§08 safe failure)."""
    from app import main as main_module

    def exploding_interpret(notes, battery, client=None):
        raise main_module.interpret.InterpretationError("provider down")

    monkeypatch.setattr(main_module.interpret, "interpret_notes", exploding_interpret)
    response = client.post("/optimize-energy", json=public_cases[0]["input"])
    assert response.status_code == 200, response.text

    body = response.json()
    assert all(e["directive_type"] == "no_op" for e in body["directive_interpretation"])
    assert len(body["hourly_plan"]) == 24


def test_route_is_described_without_leaking_the_key(monkeypatch):
    """The degradation log names the route so a misconfiguration is visible.

    It must show where the request went without ever printing the credential.
    """
    from app import interpret

    secret = "sk-super-secret-value"
    monkeypatch.setenv("LLM_API_KEY", secret)
    described = interpret.describe_route()

    assert "::" in described and "@" in described
    assert secret not in described
    assert interpret.is_configured() is True


def test_describe_route_safe_survives_a_malformed_numeric_var(monkeypatch):
    """A bad LLM_TIMEOUT_SECONDS must not turn safe failure into a 500 (§08).

    ``_settings`` parses two numerics. On the degradation path those are read
    *inside* the exception handler, so an unguarded parse error would escape as
    an unhandled exception instead of the no_op fallback.
    """
    from app import interpret

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "not-a-number")
    with pytest.raises(ValueError):
        interpret.describe_route()

    assert "unresolved" in interpret.describe_route_safe()


def test_malformed_numeric_var_still_degrades_to_no_op(client, public_cases, monkeypatch):
    """End to end: the malformed var reaches the handler but still returns 200."""
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "not-a-number")
    response = client.post("/optimize-energy", json=public_cases[0]["input"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert all(e["directive_type"] == "no_op" for e in body["directive_interpretation"])
    assert len(body["hourly_plan"]) == 24
