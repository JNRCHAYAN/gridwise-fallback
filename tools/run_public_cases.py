#!/usr/bin/env python3
"""Run the public sample cases against the pipeline.

Two modes:

  --offline   Feed the pack's ground-truth directive interpretations straight
              into guardrails -> optimizer -> replay. Isolates the math from
              the language model, so a failure here is always a solver or
              accounting bug, never a prompt bug.

  --live      POST each case to a running service and validate the response
              end to end, including the LLM interpretation path.

Usage:
    python tools/run_public_cases.py --offline
    python tools/run_public_cases.py --live --url http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.guardrails import compile_directives, validate_interpretation  # noqa: E402
from app.optimizer import optimize  # noqa: E402
from app.replay import (  # noqa: E402
    check_directive_compliance,
    recompute_totals,
    verify_plan,
)

# §02 requires that organisers can run the public-sample check from the submitted
# repository alone, so a copy of the pack ships in tools/. When the official file
# sits beside the project (as it does during the round) that copy wins, since it
# is the one the organisers actually distributed.
_VENDORED_PACK = ROOT / "tools" / "public_sample_cases.json"
_EXTERNAL_PACK = ROOT.parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
DEFAULT_PACK = _EXTERNAL_PACK if _EXTERNAL_PACK.exists() else _VENDORED_PACK
TOLERANCE = 0.01


def load_pack(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)["cases"]


def _battery_to_dict(battery: dict) -> dict:
    return {k: float(v) for k, v in battery.items()}


def solve_case(case: dict, raw_entries: list[dict]) -> dict:
    """Run the deterministic half of the pipeline for one case."""
    request = case["input"]
    hours = request["hours"]
    battery = _battery_to_dict(request["battery"])

    entries, warnings = validate_interpretation(
        raw_entries, len(request["operator_notes"]), battery["capacity_kwh"]
    )

    base_solar = [float(h["solar_kwh"]) for h in hours]
    compiled = compile_directives(
        entries, base_solar, battery["minimum_energy_kwh"], battery["capacity_kwh"]
    )

    plan, solver_warnings = optimize(hours, battery, compiled, base_solar)

    return {
        "entries": entries,
        "plan": plan,
        "compiled": compiled,
        "warnings": warnings + solver_warnings,
        "violations": verify_plan(plan, hours, battery, compiled),
        "directive_violations": check_directive_compliance(plan, entries),
        "totals": recompute_totals(plan, hours),
        "battery": battery,
        "hours": hours,
    }


def run_offline(pack_path: Path) -> int:
    cases = load_pack(pack_path)
    print(f"Offline pipeline check — {len(cases)} public cases")
    print("=" * 78)

    failures = 0
    quality_sum = 0.0

    for case in cases:
        expected = case["expected_output"]
        result = solve_case(case, expected["directive_interpretation"])

        cost = result["totals"]["total_cost_bdt"]
        reference_cost = float(expected["total_cost_bdt"])
        ratio = 1.0 if reference_cost <= TOLERANCE else min(1.0, reference_cost / max(cost, 1e-9))
        quality_sum += ratio

        problems = result["violations"] + result["directive_violations"]
        if cost > reference_cost + TOLERANCE:
            problems.append(f"cost {cost:.2f} worse than reference {reference_cost:.2f}")

        status = "PASS" if not problems else "FAIL"
        if problems:
            failures += 1

        print(
            f"{case['id']}  {status}  cost={cost:10.2f}  reference={reference_cost:10.2f}  "
            f"ratio={ratio:5.3f}"
        )
        for problem in problems[:6]:
            print(f"      ! {problem}")
        for warning in result["warnings"][:3]:
            print(f"      ~ {warning}")

    print("=" * 78)
    average_quality = quality_sum / len(cases) if cases else 0.0
    print(f"passed {len(cases) - failures}/{len(cases)}   "
          f"mean optimization ratio {average_quality:.4f} "
          f"(=> {average_quality * 10:.2f}/10 optimization points)")
    return 1 if failures else 0


def run_live(pack_path: Path, url: str) -> int:
    import httpx

    cases = load_pack(pack_path)
    print(f"Live end-to-end check against {url} — {len(cases)} public cases")
    print("=" * 78)

    failures = 0
    with httpx.Client(timeout=30.0) as client:
        try:
            health = client.get(f"{url.rstrip('/')}/health")
            print(f"/health -> {health.status_code} {health.text.strip()}")
            if health.status_code != 200:
                print("service is not healthy; aborting")
                return 1
        except Exception as exc:  # noqa: BLE001
            print(f"could not reach service: {exc}")
            return 1

        for case in cases:
            request = case["input"]
            expected = case["expected_output"]
            try:
                response = client.post(f"{url.rstrip('/')}/optimize-energy", json=request)
            except Exception as exc:  # noqa: BLE001
                print(f"{case['id']}  FAIL  request error: {exc}")
                failures += 1
                continue

            if response.status_code != 200:
                print(f"{case['id']}  FAIL  HTTP {response.status_code}: {response.text[:200]}")
                failures += 1
                continue

            body = response.json()
            problems: list[str] = []

            # ---- interpretation compared against ground truth -------------
            got = {e["note_index"]: e for e in body["directive_interpretation"]}
            for want in expected["directive_interpretation"]:
                index = want["note_index"]
                have = got.get(index)
                if have is None:
                    problems.append(f"missing interpretation for note {index}")
                    continue
                if have["directive_type"] != want["directive_type"]:
                    problems.append(
                        f"note {index}: type {have['directive_type']} != {want['directive_type']}"
                    )
                    continue
                if have["applies"] != want["applies"]:
                    problems.append(f"note {index}: applies mismatch")
                if want["structured_adjustment"] and have.get("structured_adjustment"):
                    for key, value in want["structured_adjustment"].items():
                        actual = have["structured_adjustment"].get(key)
                        if key == "hours":
                            if list(actual or []) != list(value):
                                problems.append(f"note {index}: hours {actual} != {value}")
                        elif actual is None or abs(float(actual) - float(value)) > TOLERANCE:
                            problems.append(f"note {index}: {key} {actual} != {value}")

            # ---- replay the returned plan ---------------------------------
            battery = _battery_to_dict(request["battery"])
            hours = request["hours"]
            entries, _ = validate_interpretation(
                body["directive_interpretation"],
                len(request["operator_notes"]),
                battery["capacity_kwh"],
            )
            base_solar = [float(h["solar_kwh"]) for h in hours]
            compiled = compile_directives(
                entries, base_solar, battery["minimum_energy_kwh"], battery["capacity_kwh"]
            )
            problems += verify_plan(body["hourly_plan"], hours, battery, compiled)

            # ---- totals must match the plan -------------------------------
            recalculated = recompute_totals(body["hourly_plan"], hours)
            for key, value in recalculated.items():
                if abs(float(body[key]) - value) > TOLERANCE:
                    problems.append(f"{key} {body[key]} != recalculated {value}")

            cost = float(body["total_cost_bdt"])
            reference_cost = float(expected["total_cost_bdt"])
            if cost > reference_cost + TOLERANCE:
                problems.append(f"cost {cost:.2f} worse than reference {reference_cost:.2f}")

            status = "PASS" if not problems else "FAIL"
            if problems:
                failures += 1

            print(f"{case['id']}  {status}  cost={cost:10.2f}  reference={reference_cost:10.2f}")
            for problem in problems[:8]:
                print(f"      ! {problem}")

    print("=" * 78)
    print(f"passed {len(cases) - failures}/{len(cases)}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true",
                        help="use ground-truth directives; exercises math only")
    parser.add_argument("--live", action="store_true",
                        help="POST to a running service")
    parser.add_argument("--url", default=os.environ.get("GRIDWISE_URL", "http://localhost:8000"))
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    args = parser.parse_args()

    pack_path = Path(args.pack)
    if not pack_path.exists():
        print(f"sample pack not found: {pack_path}")
        return 2

    if args.live:
        return run_live(pack_path, args.url)
    return run_offline(pack_path)


if __name__ == "__main__":
    raise SystemExit(main())
