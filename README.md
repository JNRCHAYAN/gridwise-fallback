# GridWise LLM — Smart Campus Energy Optimization

**BUP CSE Fest 2026 · Hackathon · Online Preliminary**

An HTTP API that reads natural-language operator notes about a campus energy
scenario, converts them into validated machine-checkable directives, applies
them to a constrained optimization model, and returns the cheapest valid
24-hour electricity schedule.

```
POST /optimize-energy
   operator_notes  ──►  LLM interpretation  ──►  deterministic guardrails
                                                       │
                                                       ▼
                              replay verification  ◄── LP optimizer
                                                       │
                                                       ▼
                                        interpretation + 24-hour plan
```

The language model is the **only** component that decides what a note means.
Everything downstream is deterministic code, because model output is never
trusted until it has been validated.

---

## Quickstart (fresh environment)

Requires **Python 3.11+** (developed on 3.12). Nothing else.

```bash
# 1. Clone and enter
git clone <repository-url>
cd gridwise

# 2. Create an isolated environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
pip install pytest                  # only needed to run the test suite

# 4. Configure the model provider (see "Configuration" below)
cp .env.example .env                # then edit .env and set LLM_API_KEY

# 5. Start the service
export $(grep -v '^#' .env | xargs)   # Windows: load the vars into your shell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Verify it is up:

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}
```

Call the main endpoint with one of the public sample cases:

```bash
curl -s -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "GRID-101",
    "operator_notes": [
      "Solar output will drop to about 20% from 1 PM to 3 PM.",
      "Do not charge the battery between 2 PM and 4 PM.",
      "The cafeteria menu changes tomorrow."
    ],
    "hours": [
      {"hour": 0,  "demand_kwh": 180, "solar_kwh": 0,   "tariff_bdt_per_kwh": 7},
      {"hour": 1,  "demand_kwh": 175, "solar_kwh": 0,   "tariff_bdt_per_kwh": 7},
      {"hour": 2,  "demand_kwh": 170, "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
      {"hour": 3,  "demand_kwh": 170, "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
      {"hour": 4,  "demand_kwh": 175, "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
      {"hour": 5,  "demand_kwh": 185, "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
      {"hour": 6,  "demand_kwh": 200, "solar_kwh": 5,   "tariff_bdt_per_kwh": 8},
      {"hour": 7,  "demand_kwh": 220, "solar_kwh": 20,  "tariff_bdt_per_kwh": 10},
      {"hour": 8,  "demand_kwh": 240, "solar_kwh": 50,  "tariff_bdt_per_kwh": 12},
      {"hour": 9,  "demand_kwh": 255, "solar_kwh": 90,  "tariff_bdt_per_kwh": 14},
      {"hour": 10, "demand_kwh": 265, "solar_kwh": 130, "tariff_bdt_per_kwh": 16},
      {"hour": 11, "demand_kwh": 270, "solar_kwh": 160, "tariff_bdt_per_kwh": 16},
      {"hour": 12, "demand_kwh": 275, "solar_kwh": 180, "tariff_bdt_per_kwh": 15},
      {"hour": 13, "demand_kwh": 270, "solar_kwh": 170, "tariff_bdt_per_kwh": 14},
      {"hour": 14, "demand_kwh": 260, "solar_kwh": 140, "tariff_bdt_per_kwh": 13},
      {"hour": 15, "demand_kwh": 255, "solar_kwh": 90,  "tariff_bdt_per_kwh": 14},
      {"hour": 16, "demand_kwh": 260, "solar_kwh": 45,  "tariff_bdt_per_kwh": 18},
      {"hour": 17, "demand_kwh": 275, "solar_kwh": 10,  "tariff_bdt_per_kwh": 22},
      {"hour": 18, "demand_kwh": 295, "solar_kwh": 0,   "tariff_bdt_per_kwh": 28},
      {"hour": 19, "demand_kwh": 305, "solar_kwh": 0,   "tariff_bdt_per_kwh": 30},
      {"hour": 20, "demand_kwh": 295, "solar_kwh": 0,   "tariff_bdt_per_kwh": 26},
      {"hour": 21, "demand_kwh": 265, "solar_kwh": 0,   "tariff_bdt_per_kwh": 18},
      {"hour": 22, "demand_kwh": 225, "solar_kwh": 0,   "tariff_bdt_per_kwh": 10},
      {"hour": 23, "demand_kwh": 195, "solar_kwh": 0,   "tariff_bdt_per_kwh": 7}
    ],
    "battery": {
      "capacity_kwh": 500,
      "initial_energy_kwh": 200,
      "minimum_energy_kwh": 50,
      "max_charge_kwh_per_hour": 100,
      "max_discharge_kwh_per_hour": 100
    }
  }'
```

Or run the whole public pack end to end:

```bash
python tools/run_public_cases.py --offline    # math only, no model needed
python tools/run_public_cases.py --live       # against a running service
```

---

## Public sample validation

`tools/run_public_cases.py --offline` runs the 10 official public cases through
the guardrail, optimizer and replay layers using the pack's **ground-truth**
directive interpretations. This isolates the math from the language model, so a
failure here is always a solver or accounting bug, never a prompt bug.

Expected result:

```
Offline pipeline check — 10 public cases
==============================================================================
SAMPLE-01  PASS  cost=  38365.00  reference=  38365.00  ratio=1.000
SAMPLE-02  PASS  cost=  42885.00  reference=  42885.00  ratio=1.000
...
SAMPLE-10  PASS  cost=  41620.00  reference=  41620.00  ratio=1.000
==============================================================================
passed 10/10   mean optimization ratio 1.0000 (=> 10.00/10 optimization points)
```

Every case matches the organiser's reference cost exactly, because the
optimizer solves the linear program to true optimality rather than
approximating it.

`--live` additionally exercises the language model and compares the returned
interpretation against the pack's ground truth.

Unit tests:

```bash
pytest -q
```

The suite stubs the model call, so it runs offline and never depends on a
provider being reachable.

---

## Docker fallback

The image is self-contained and contains **no credentials**.

```bash
# Pull
docker pull <registry>/<image>:<tag>

# Run (supply the key at run time)
docker run --rm -p 8000:8000 \
  -e LLM_API_KEY=your-key-here \
  -e LLM_BASE_URL=https://capi.aerolink.lat \
  -e LLM_MODEL=claude-haiku-4-5-20251001 \
  <registry>/<image>:<tag>

# Verify
curl -s http://localhost:8000/health
# {"status":"ok"}
```

Build locally instead:

```bash
docker build -t gridwise-llm:local .
docker run --rm -p 8000:8000 -e LLM_API_KEY=... gridwise-llm:local
```

The container binds to `0.0.0.0` and listens on port `8000` (`PORT` overridable).

---

## Configuration

All configuration is via environment variables. Names only — no values are
committed anywhere in this repository.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `LLM_API_KEY` | **yes** | — | Credential for the interpretation model |
| `LLM_BASE_URL` | no | `https://capi.aerolink.lat` | Provider base URL |
| `LLM_MODEL` | no | `claude-haiku-4-5-20251001` | Model used to read operator notes |
| `LLM_API_STYLE` | no | `anthropic` | `anthropic` or `openai` wire format |
| `LLM_TIMEOUT_SECONDS` | no | `20` | Timeout for the model call |
| `LLM_MAX_TOKENS` | no | `1500` | Response token budget |
| `PORT` | no | `8000` | Listening port (container) |
| `LOG_LEVEL` | no | `INFO` | Log verbosity |

**Model / provider.** Operator-note interpretation uses an
Anthropic-Messages-compatible model by default. `LLM_API_STYLE=openai` switches
to the OpenAI chat-completions shape, which covers Ollama, vLLM and most other
providers — so a local model can be substituted without code changes.

Swapping the provider is three environment variables. For DeepSeek:

```bash
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_STYLE=openai
```

`app/interpret.py` selects the wire format from `LLM_API_STYLE` alone; there is
no provider-specific code path to edit.

---

## Architecture

### Why the LLM is where it is

The Problem Statement requires a language-capable model **in the
interpretation path**, and disqualifies hard-coded phrase matching as the sole
interpreter. This implementation satisfies that by making the model the only
component that maps language to meaning:

- `app/interpret.py` — builds the prompt, calls the model, parses JSON.
  Contains **no** directive logic.
- `app/guardrails.py` — validates and normalises everything the model returns.
  Contains **no** model calls.
- `app/optimizer.py` — pure linear programming. Never sees raw model output.
- `app/replay.py` — independently re-verifies the produced schedule.

A judge reading the repository can follow the chain directly:
`operator_notes → interpret_notes() → validate_interpretation() →
compile_directives() → optimize() → verify_plan()`.

### Guardrails (`app/guardrails.py`)

Model output is untrusted. Before anything reaches the optimizer:

- `directive_type` must be one of the five supported directives or `no_op`
- exactly one entry per note, ordered `0..N-1`, no missing or duplicate mappings
- `hours` must be unique integers in `0..23`, ascending
- `factor` must lie in `[0, 1]`
- reserve and grid-cap values must be finite, non-negative, and within capacity
- `no_op` must use `applies=false` and a null adjustment; every other directive
  must use `applies=true`

**Rejected entries degrade to `no_op`** rather than being silently repaired into
a different directive. That costs interpretation credit for the affected note
but keeps the response schema-valid and the schedule feasible — strictly better
than crashing or inventing a constraint the operator never asked for.

### Optimizer (`app/optimizer.py`)

The scheduling problem is entirely linear, so it is solved exactly with
`scipy.optimize.linprog` (HiGHS) rather than approximated with a heuristic.

Decision variables per hour: `grid[h]`, `solar_used[h]`, `charge[h]`,
`discharge[h]` — 96 variables total.

```
minimise   Σ grid[h] × tariff[h]

subject to energy balance   grid + solar_used + discharge − charge = demand
           battery bounds   floor[h] ≤ E[h] ≤ capacity
           rate limits      charge[h] ≤ max_charge,  discharge[h] ≤ max_discharge
           solar limit      solar_used[h] ≤ effective_solar[h]
           neutrality       E[23] = E[0]
           directives       windows and caps from the guardrail layer
```

Solving to true optimality makes the organiser's cost ratio
(`organizer_optimal / our_cost`) exactly `1.0` on every case.

**Fallback ladder.** If the directive set is jointly infeasible, the optimizer
drops all directives and re-solves against the base scenario; if even that
fails, it returns an idle-battery schedule that is valid by construction. The
service never raises, and never returns a 500 for a well-formed request.

### Replay verification (`app/replay.py`)

The judge independently replays the returned schedule. This module does the
same thing to our own output *before* it is returned — re-deriving battery
state, energy balance, bounds, rate limits and every directive constraint from
the emitted plan. Reported totals are recalculated from `hourly_plan` itself,
so they cannot disagree with it.

### Safe failure

If the model is unreachable or returns unparseable output, every note degrades
to `no_op`, a valid schedule is still produced, and the response remains
schema-correct. A dead provider never takes the service down.

---

## Dependencies

| Package | Role | Licence |
|---|---|---|
| `fastapi` | HTTP framework, request/response validation | MIT |
| `uvicorn` | ASGI server | BSD-3-Clause |
| `pydantic` | Schema validation | MIT |
| `numpy` | LP matrix construction | BSD-3-Clause |
| `scipy` | `linprog` / HiGHS linear solver | BSD-3-Clause |
| `httpx` | Model provider HTTP client | BSD-3-Clause |
| `pytest` | Test suite (development only) | MIT |

No solver binaries are bundled; HiGHS ships inside `scipy`.

---

## API reference

### `GET /health`

```json
{"status": "ok"}
```

### `POST /optimize-energy`

Accepts one scenario object: `scenario_id`, `operator_notes` (1–3 strings),
`hours` (exactly 24 entries, hours 0–23), `battery`.

Returns `scenario_id`, `directive_interpretation` (one entry per note, in
`note_index` order), `hourly_plan` (24 entries), `total_grid_kwh`,
`total_cost_bdt`, `peak_grid_kwh`, and a short `plan_summary`.

| Status | Meaning |
|---|---|
| 200 | Success |
| 400 | Malformed JSON, or a structurally invalid body: missing field, wrong type, wrong number of hours or notes, unexpected extra field (§6.1) |
| 422 | A correctly shaped body whose *values* fall outside their allowed range, e.g. a non-positive battery capacity (§6.1, optional code) |
| 500 | Controlled internal error — never exposes a stack trace or secret |

The distinction follows §6.1: 400 covers "malformed JSON or structurally invalid
request", while 422 is reserved for a request that is well-formed but
semantically invalid. A body carrying 23 hours is a 400, not a 422.

### Trying it from the browser

`GET /docs` serves Swagger UI with a **complete, valid 24-hour scenario**
pre-filled into `POST /optimize-energy`, so *Try it out → Execute* returns a
real plan with no editing. The example is the first case of the organiser's
public sample pack, and a regression test asserts it stays both schema-valid and
accepted by the live endpoint.

```bash
curl -s http://localhost:8000/docs      # then pick POST /optimize-energy
```

---

## Known limitations

- **Interpretation quality is bounded by the model.** Ambiguous notes are
  deliberately downgraded to `no_op` rather than guessed at, which is the
  conservative choice but forgoes credit if the organiser intended a directive.
- **The optimizer assumes organiser scenarios are feasible**, as the Problem
  Statement guarantees. Contradictory directive sets fall back to a
  base-rules schedule and lose directive-application credit for that case.
- **No response caching.** Every request performs a fresh model call. Repeated
  identical scenarios are re-interpreted each time.
- **Percentages are converted by the model**, using the battery capacity
  supplied in the prompt. An unusually-worded percentage could be mis-scaled.
- The `--live` validation mode requires a reachable service and a valid key.

---

## Secret handling

- No credentials, tokens or `.env` files are committed. `.env` is gitignored.
- The Docker image bakes in no secrets; `LLM_API_KEY` is supplied at run time.
- Logs record scenario ids, note counts, costs and timings — never key
  material, prompts containing secrets, or raw stack traces.
- Error responses return a generic message; exception detail is logged
  server-side only.

---

## Repository layout

```
gridwise/
├── app/
│   ├── main.py          FastAPI service and request orchestration
│   ├── schemas.py       Pydantic request/response models
│   ├── examples.py      worked request body served to Swagger UI at /docs
│   ├── interpret.py     LLM prompt, provider call, JSON parsing
│   ├── guardrails.py    deterministic validation of model output
│   ├── optimizer.py     linear program construction and solve
│   └── replay.py        independent verification and total recalculation
├── tests/
│   └── test_pipeline.py unit, contract and public-case tests
├── tools/
│   └── run_public_cases.py   offline and live sample-pack runner
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Attribution

Core architecture, optimization model, guardrail design and implementation are
the team's own work. Third-party libraries are listed under **Dependencies**.
AI coding assistants were used during development, consistent with the
competition rulebook.
