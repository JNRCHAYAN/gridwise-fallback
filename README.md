# GridWise LLM — Smart Campus Energy Optimization

**BUP CSE Fest 2026 · Online Preliminary · LLM-Assisted Operator Directive Interpretation**

An HTTP API that reads natural-language operator notes about a campus energy
scenario, converts each note into a validated machine-checkable directive with a
language model, applies those directives to a constrained optimisation model,
and returns the cheapest valid 24-hour electricity schedule.

```
POST /optimize-energy
   operator_notes ──► LLM interpretation ──► deterministic guardrails
                                                    │
                                                    ▼
                            replay verification ◄── LP optimizer (HiGHS)
                                                    │
                                                    ▼
                                     interpretation + 24-hour plan
```

The language model is the **only** component that decides what a note means.
Everything downstream is deterministic code, because model output is never
trusted until it has been validated.

---

## Live deployment

| | |
|---|---|
| **Base URL** | `https://gridwise-fallback.onrender.com` |
| **Health** | `GET https://gridwise-fallback.onrender.com/health` |
| **Primary** | `POST https://gridwise-fallback.onrender.com/optimize-energy` |
| **Interactive docs** | `https://gridwise-fallback.onrender.com/docs` |

Both endpoints are public — no login, dashboard, VPN or approval is required.
`/docs` serves Swagger UI with a complete, valid 24-hour scenario pre-filled, so
*Try it out → Execute* returns a real plan with no editing.

> **Cold starts.** The hosted instance runs on a free tier that scales to zero
> after 15 minutes idle; the first request after that takes ~50 s. A keep-alive
> probe against `/health` runs every 10 minutes to keep it warm. If you hit a
> slow first request, retry once — subsequent requests return in ~1.5 s.

---

## Quickstart (clean environment)

Requires **Python 3.11+** (developed and verified on 3.12) and nothing else.
No database, no build step, no system packages.

```bash
# 1. Clone
git clone https://github.com/JNRCHAYAN/gridwise-fallback.git
cd gridwise-fallback

# 2. Isolated environment
python -m venv .venv
source .venv/bin/activate           # Windows PowerShell: .venv\Scripts\Activate.ps1

# 3. Dependencies
pip install -r requirements.txt

# 4. Configure the model credential
cp .env.example .env
#    then open .env and set LLM_API_KEY to a DeepSeek API key.
#    The provider route is already filled in and matches the deployment.

# 5. Start the service
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The service loads `.env` automatically at startup. A variable already set in the
real environment always takes precedence over `.env`, so the same file cannot
override a deployed container's configuration.

**Verify it is up** (in a second terminal):

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}
```

**Run one public sample against the live service:**

```bash
curl -s -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @tools/sample_request.json
```

`tools/sample_request.json` ships in the repository and is byte-identical to
case `SAMPLE-01` of the organiser's public pack, so there is nothing to prepare.

Expected: HTTP 200, `directive_interpretation` holding one entry per note — note
0 `solar_reduction` with `hours: [12, 13]`, `factor: 0.25`; note 1 `no_op` with
`applies: false` and a null adjustment — and `total_cost_bdt: 38365.0`, matching
the organiser's reference for this case.

The complete request body and the full response are in
[Sample request and response](#sample-request-and-response) below.

---

## Public sample validation

The organiser's public pack ships in this repository at
`tools/public_sample_cases.json`, so the checks below run from the repository
alone without any external file.

### Offline — validates the deterministic half (no API key needed)

```bash
python tools/run_public_cases.py --offline
```

This feeds the pack's **ground-truth** directive interpretations straight into
guardrails → optimizer → replay. It isolates the maths from the language model,
so a failure here is always a solver or accounting bug, never a prompt bug.

**Expected result — exactly this, all ten cases PASS:**

```
Offline pipeline check — 10 public cases
==============================================================================
SAMPLE-01  PASS  cost=  38365.00  reference=  38365.00  ratio=1.000
SAMPLE-02  PASS  cost=  42885.00  reference=  42885.00  ratio=1.000
SAMPLE-03  PASS  cost=  35480.00  reference=  35480.00  ratio=1.000
SAMPLE-04  PASS  cost=  40495.00  reference=  40495.00  ratio=1.000
SAMPLE-05  PASS  cost=  33950.00  reference=  33950.00  ratio=1.000
SAMPLE-06  PASS  cost=  34090.00  reference=  34090.00  ratio=1.000
SAMPLE-07  PASS  cost=  38550.00  reference=  38550.00  ratio=1.000
SAMPLE-08  PASS  cost=  37665.00  reference=  37665.00  ratio=1.000
SAMPLE-09  PASS  cost=  34873.00  reference=  34873.00  ratio=1.000
SAMPLE-10  PASS  cost=  41620.00  reference=  41620.00  ratio=1.000
==============================================================================
passed 10/10   mean optimization ratio 1.0000 (=> 10.00/10 optimization points)
```

Every case matches the organiser's reference cost exactly, because the optimiser
solves the linear program to true optimality rather than approximating it.

### Live — validates the full pipeline including the model

```bash
# against a local service
python tools/run_public_cases.py --live --url http://localhost:8000

# against the deployed service
python tools/run_public_cases.py --live --url https://gridwise-fallback.onrender.com
```

This additionally exercises the language model and compares the returned
`directive_interpretation` against the pack's ground truth, then independently
replays the returned `hourly_plan`. Expected: `passed 10/10`.

### Unit tests

```bash
pip install pytest
pytest -q          # 59 passed
```

The suite stubs the model call, so it runs offline and never depends on a
provider being reachable. It covers the request/response contract, the §6.1
status-code split, guardrail behaviour on malformed model output, solver
correctness, replay verification, and the published OpenAPI document.

---

## Docker fallback

The image is self-contained, listens on `0.0.0.0`, and contains **no
credentials** — the API key is supplied at run time.

### Build and run (verified)

```bash
# Build
docker build -t gridwise-llm:1.0.0 .

# Run — supplying the key alone is enough; the route defaults are baked in
docker run --rm -p 8000:8000 -e LLM_API_KEY=your-key-here gridwise-llm:1.0.0

# Verify (second terminal)
curl -s http://localhost:8000/health
# {"status":"ok"}
```

Override the port or the model route with `-e`:

```bash
docker run --rm -p 9000:9000 \
  -e PORT=9000 \
  -e LLM_API_KEY=your-key-here \
  -e LLM_API_STYLE=anthropic \
  -e LLM_BASE_URL=https://capi.aerolink.lat \
  -e LLM_MODEL=claude-haiku-4-5-20251001 \
  gridwise-llm:1.0.0
```

### Registry reference

The build above is the verified local path. The organizer-facing fallback image
is published at:

```
docker pull <REGISTRY>/gridwise-llm:1.0.0
docker run --rm -p 8000:8000 -e LLM_API_KEY=your-key-here <REGISTRY>/gridwise-llm:1.0.0
```

| | |
|---|---|
| Image reference | `<REGISTRY>/gridwise-llm:1.0.0` |
| Exposed port | `8000` (override with `-e PORT=...`) |
| Required env var | `LLM_API_KEY` |
| Optional env vars | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_STYLE`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_TOKENS`, `PORT`, `LOG_LEVEL` |
| Baked secrets | none |

**Container defaults.** The image sets `LLM_API_STYLE=openai`,
`LLM_BASE_URL=https://api.deepseek.com` and `LLM_MODEL=deepseek-chat` — the route
used by the submitted deployment. These are not secrets and every one is
overridable with `-e`. They are set deliberately: a *partial* configuration is
the dangerous case, because a key present with the route left to defaults is
sent to the wrong provider and fails silently (see
[Known limitations](#known-limitations)).

---

## Configuration

All configuration is by environment variable. **Names only — no secret values
are committed anywhere in this repository.**

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `LLM_API_KEY` | **yes** | — | Credential for the interpretation model |
| `LLM_BASE_URL` | no | `https://api.deepseek.com` | Provider base URL |
| `LLM_MODEL` | no | `deepseek-chat` | Model that reads operator notes |
| `LLM_API_STYLE` | no | `openai` | `openai` or `anthropic` wire format |
| `LLM_TIMEOUT_SECONDS` | no | `20` | Timeout for the model call |
| `LLM_MAX_TOKENS` | no | `1500` | Response token budget |
| `PORT` | no | `8000` | Listening port |
| `LOG_LEVEL` | no | `INFO` | Log verbosity |

### Model and provider used

Operator-note interpretation uses a **language-capable generative model** over a
hosted HTTP API. The submitted deployment runs:

| | |
|---|---|
| Provider | **DeepSeek** |
| Model identifier | **`deepseek-chat`** |
| Wire format | OpenAI chat-completions (`LLM_API_STYLE=openai`) |
| Endpoint | `https://api.deepseek.com/v1/chat/completions` |

Switching provider needs **no code change** — `app/interpret.py` selects the wire
format from `LLM_API_STYLE` alone. Three variables cover any
OpenAI-compatible or Anthropic-Messages-compatible endpoint:

```bash
# Anthropic-Messages-compatible provider
LLM_BASE_URL=https://capi.aerolink.lat
LLM_MODEL=claude-haiku-4-5-20251001
LLM_API_STYLE=anthropic

# Local Ollama — no external provider, no cost, satisfies the same requirement
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=llama3.1
LLM_API_STYLE=openai
LLM_API_KEY=ollama
```

`LLM_API_KEY` must be non-empty for any provider: without it the service starts
normally but degrades every note to `no_op` (see
[Safe failure](#safe-failure)).

---

## Architecture

### The LLM's role, and why it sits exactly there

The Problem Statement makes a language-capable model **mandatory in the
operator-note interpretation path**, and rules out hard-coded phrase matching as
the sole interpreter. This implementation satisfies that structurally: the model
is the only component that maps language to meaning, and its structured output is
what produces the optimisation constraints.

| Module | Responsibility | Model calls? | Directive logic? |
|---|---|---|---|
| `app/interpret.py` | prompt, provider call, JSON extraction | **yes** | none |
| `app/guardrails.py` | validate and normalise model output | no | validation only |
| `app/optimizer.py` | build and solve the linear program | no | none |
| `app/replay.py` | independently re-verify the emitted plan | no | none |

A reviewer can follow the chain directly in the source:

```
operator_notes
  → interpret_notes()          LLM reads the notes, returns raw JSON
  → validate_interpretation()  deterministic guardrails; untrusted input stops here
  → compile_directives()       directives → LP constraints
  → optimize()                 exact solve
  → verify_plan()              independent replay of our own answer
  → response
```

### Guardrails (`app/guardrails.py`)

Model output is untrusted. Before anything reaches the optimizer:

- `directive_type` must be one of the five supported directives, or `no_op`
- exactly one entry per note, ordered `0..N-1` — no missing, duplicate or
  out-of-order mappings
- `hours` must be unique integers in `0..23`, ascending, using the whole-hour
  convention (start included, end excluded: "1 PM to 3 PM" → `[13, 14]`)
- `factor` must lie in `[0, 1]` — it is the usable fraction **remaining**, so an
  "80% reduction" is `0.2`
- reserve and grid-cap values must be finite, non-negative and within capacity
- `no_op` must use `applies=false` and a null adjustment; every other directive
  must use `applies=true`

**Rejected entries degrade to `no_op`** rather than being silently repaired into
a different directive. That costs interpretation credit for the affected note but
keeps the response schema-valid and the schedule feasible — strictly better than
crashing, or inventing a constraint the operator never asked for.

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
           directives       windows and caps produced by the guardrail layer
```

`solar_reduction` is applied to the *effective* solar available before the
schedule is checked, so the reduction changes what the plan is allowed to use.

Solving to true optimality is what makes the organiser's cost ratio
(`organizer_optimal / our_cost`) exactly `1.0` on every public case.

**Fallback ladder.** If the directive set is jointly infeasible, the optimizer
drops all directives and re-solves against the base scenario; if even that fails
it returns an idle-battery schedule that is valid by construction. The service
never returns a 500 for a well-formed request.

### Replay verification (`app/replay.py`)

The judge independently replays the returned schedule. This module does the same
to our own output *before* it is returned — re-deriving battery state, energy
balance, bounds, rate limits and every directive constraint from the emitted
plan. Reported totals are recalculated **from `hourly_plan` itself**, so they
cannot disagree with it.

### Safe failure

If the model is unreachable or returns unparseable output, every note degrades to
`no_op`, a valid schedule is still produced, and the response stays
schema-correct. A dead provider never takes the service down. The resolved model
route is logged alongside the error so that a misconfiguration is
distinguishable from a genuine provider outage.

---

## API reference

### `GET /health`

```bash
curl -s http://localhost:8000/health
```

```json
{"status": "ok"}
```

Returns `200` with `{"status":"ok"}` (§6.2), within 60 seconds of service start.

### `POST /optimize-energy`

Accepts one scenario: `scenario_id`, `operator_notes` (1–3 strings), `hours`
(exactly 24 entries, hours 0–23), `battery`.

Returns `scenario_id`, `directive_interpretation` (exactly one entry per note, in
`note_index` order), `hourly_plan` (24 entries), `total_grid_kwh`,
`total_cost_bdt`, `peak_grid_kwh`, and a short `plan_summary`.

```bash
curl -s -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @tools/sample_request.json
```

The complete 24-hour body is in
[Sample request and response](#sample-request-and-response) below, and
`docs/DEPLOYMENT.md` covers hosting this service on a public platform.

| Status | Meaning |
|---|---|
| 200 | Success |
| 400 | Malformed JSON, or a structurally invalid body: missing field, wrong type, wrong number of hours or notes, unexpected extra field (§6.1) |
| 422 | A correctly shaped body whose *values* fall outside their allowed range, e.g. a non-positive battery capacity (§6.1, optional code) |
| 500 | Controlled internal error — never exposes a stack trace or secret |

The split follows §6.1: `400` covers "malformed JSON or structurally invalid
request"; `422` is reserved for a well-formed body that is semantically invalid.
A body carrying 23 hours is a `400`, not a `422`. Error bodies are always
`{"error": "...", "detail": [...]}` and never contain a stack trace or secret.

---

## Sample request and response

Full request body — this is `tools/sample_request.json`, the first case of the
organiser's public pack, reproduced here so the contract is readable in one
place:

```bash
curl -s -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @sample.json
```

```json
{
  "scenario_id": "SAMPLE-01",
  "operator_notes": [
    "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
    "The sports office moved next month's registration deadline."
  ],
  "hours": [
    {"hour": 0,  "demand_kwh": 90,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 1,  "demand_kwh": 85,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 2,  "demand_kwh": 80,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
    {"hour": 3,  "demand_kwh": 80,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
    {"hour": 4,  "demand_kwh": 85,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
    {"hour": 5,  "demand_kwh": 95,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 6,  "demand_kwh": 110, "solar_kwh": 5,   "tariff_bdt_per_kwh": 8},
    {"hour": 7,  "demand_kwh": 130, "solar_kwh": 20,  "tariff_bdt_per_kwh": 10},
    {"hour": 8,  "demand_kwh": 150, "solar_kwh": 50,  "tariff_bdt_per_kwh": 12},
    {"hour": 9,  "demand_kwh": 165, "solar_kwh": 90,  "tariff_bdt_per_kwh": 14},
    {"hour": 10, "demand_kwh": 175, "solar_kwh": 130, "tariff_bdt_per_kwh": 16},
    {"hour": 11, "demand_kwh": 180, "solar_kwh": 160, "tariff_bdt_per_kwh": 16},
    {"hour": 12, "demand_kwh": 185, "solar_kwh": 180, "tariff_bdt_per_kwh": 15},
    {"hour": 13, "demand_kwh": 180, "solar_kwh": 170, "tariff_bdt_per_kwh": 14},
    {"hour": 14, "demand_kwh": 170, "solar_kwh": 140, "tariff_bdt_per_kwh": 13},
    {"hour": 15, "demand_kwh": 165, "solar_kwh": 90,  "tariff_bdt_per_kwh": 14},
    {"hour": 16, "demand_kwh": 170, "solar_kwh": 45,  "tariff_bdt_per_kwh": 18},
    {"hour": 17, "demand_kwh": 185, "solar_kwh": 10,  "tariff_bdt_per_kwh": 22},
    {"hour": 18, "demand_kwh": 205, "solar_kwh": 0,   "tariff_bdt_per_kwh": 28},
    {"hour": 19, "demand_kwh": 215, "solar_kwh": 0,   "tariff_bdt_per_kwh": 30},
    {"hour": 20, "demand_kwh": 205, "solar_kwh": 0,   "tariff_bdt_per_kwh": 26},
    {"hour": 21, "demand_kwh": 175, "solar_kwh": 0,   "tariff_bdt_per_kwh": 18},
    {"hour": 22, "demand_kwh": 135, "solar_kwh": 0,   "tariff_bdt_per_kwh": 10},
    {"hour": 23, "demand_kwh": 105, "solar_kwh": 0,   "tariff_bdt_per_kwh": 7}
  ],
  "battery": {
    "capacity_kwh": 220,
    "initial_energy_kwh": 110,
    "minimum_energy_kwh": 40,
    "max_charge_kwh_per_hour": 50,
    "max_discharge_kwh_per_hour": 50
  }
}
```

Response (abbreviated — `hourly_plan` carries all 24 entries):

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
      "explanation": "Solar reduced to 25% during panel cleaning noon to 2 PM"
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "Sports registration deadline is unrelated to the energy schedule"
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 40.0,
      "solar_used_kwh": 0.0,
      "battery_action": "discharge",
      "battery_kwh": 50.0,
      "battery_energy_after_kwh": 60.0
    }
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 187.5,
  "plan_summary": "Applied 1 operator directive(s): solar_reduction. Battery cycled 365.0 kWh in and 365.0 kWh out to shift energy from cheap hours into expensive ones, ending at 110.0 kWh as required. Used 827.5 kWh of solar. Total grid purchase 2692.5 kWh for 38365.00 BDT, peak 187.5 kWh."
}
```

Three details in that response are worth pointing at:

- **Note 1 is correctly non-operative.** The sports-registration note does not
  affect the schedule, so it is reported as `applies: false`,
  `directive_type: "no_op"`, `structured_adjustment: null` — the required shape.
- **The window is `[12, 13]`, not `[12, 13, 14]`.** "noon until 2 PM" uses the
  whole-hour convention: start included, end excluded. And `factor` is `0.25`,
  the fraction **remaining** — the note says usable solar is 25% of forecast.
- **`total_cost_bdt` is `38365.0`**, matching the organiser's reference exactly
  for this case. `plan_summary` is free text generated deterministically and is
  never scored on wording.

---

## Dependencies

All third-party libraries are listed here with their licences, as required.

| Package | Version | Role | Licence |
|---|---|---|---|
| `fastapi` | 0.136.3 | HTTP framework, request/response validation | MIT |
| `uvicorn[standard]` | 0.48.0 | ASGI server | BSD-3-Clause |
| `pydantic` | 2.12.5 | Request/response schema validation | MIT |
| `numpy` | 2.4.3 | LP matrix construction | BSD-3-Clause |
| `scipy` | 1.17.1 | `linprog` / HiGHS linear solver | BSD-3-Clause |
| `httpx` | 0.28.1 | Model provider HTTP client | BSD-3-Clause |
| `python-dotenv` | 1.2.3 | Loads `.env` in local development | BSD-3-Clause |
| `pytest` | any | Test suite (development only, not required to run the service) | MIT |

No solver binaries are bundled — HiGHS ships inside `scipy`. Runtime training or
fine-tuning is **not** required.

---

## Known limitations

- **A partial model configuration fails silently.** If `LLM_API_KEY` is set but
  the route variables are not, the service falls back to its built-in defaults,
  sends the key to a different provider, receives a 401, and degrades every note
  to `no_op` — while still returning HTTP 200 and a valid-looking schedule. The
  Docker image and `.env.example` therefore both ship the submitted route
  explicitly. The resolved route is logged whenever interpretation degrades, so
  this is diagnosable in one line of output.
- **Interpretation quality is bounded by the model.** Ambiguous notes are
  deliberately downgraded to `no_op` rather than guessed at. This is the
  conservative choice — it never invents a constraint — but it forgoes credit if
  the organiser intended a directive.
- **The optimizer assumes organiser scenarios are feasible**, as the Problem
  Statement guarantees. A contradictory directive set falls back to a base-rules
  schedule and loses directive-application credit for that case.
- **No response caching.** Every request performs a fresh model call; identical
  scenarios are re-interpreted each time.
- **Percentages are converted by the model** using the battery capacity supplied
  in the prompt, so an unusually-worded percentage could be mis-scaled.
- **Hosted free tier.** The deployment scales to zero after 15 minutes idle; a
  keep-alive probe mitigates this but the first request after a cold start is
  slow.
- **`--live` validation requires a reachable service and a valid key.** The
  `--offline` mode and the unit test suite need neither.

---

## Secret handling

- **No credentials are committed.** No API keys, tokens, passwords or `.env`
  files are in this repository or its history. `.env` is gitignored, and
  `.env.example` contains variable names and placeholders only.
- **The Docker image bakes in no secrets.** `.dockerignore` excludes `.env` from
  the build context, the `Dockerfile` copies only `app/` and `tools/`, and
  `LLM_API_KEY` is supplied at run time via `-e`.
- **Logs never record key material.** Request logging covers scenario id, note
  count, directive count, cost and elapsed time. Prompts and provider response
  bodies are never logged with credentials attached.
- **API responses expose no internals.** Errors return a generic message;
  exception detail and stack traces are logged server-side only and never
  returned to the client.
- **Only synthetic challenge data** supplied by the harness is used. No live
  campus, utility, billing or personal data is involved.

If you fork this repository, keep your own `.env` out of version control and
rotate any key that is ever exposed.

---

## Repository layout

```
gridwise-fallback/
├── app/
│   ├── main.py          FastAPI service: endpoints, error handling, orchestration
│   ├── schemas.py       Pydantic request/response models and published examples
│   ├── examples.py      worked 24-hour request body served to Swagger UI
│   ├── interpret.py     LLM prompt, provider call, JSON extraction  (model)
│   ├── guardrails.py    deterministic validation of model output
│   ├── optimizer.py     linear program construction and exact solve
│   └── replay.py        independent verification and total recalculation
├── tests/
│   └── test_pipeline.py unit, contract, OpenAPI and public-case tests (59)
├── tools/
│   ├── run_public_cases.py        offline and live sample-pack runner
│   ├── public_sample_cases.json   organiser's public pack, vendored
│   └── sample_request.json        ready-to-post SAMPLE-01 request body
├── docs/
│   └── DEPLOYMENT.md    hosting guide: cold starts, env vars, verification
├── Dockerfile           container image (no baked secrets)
├── .dockerignore        keeps .env and local state out of the build
├── requirements.txt     pinned dependencies
├── .env.example         variable names and placeholders only
└── README.md
```

---

## Attribution

Core architecture, the optimisation model, guardrail design and all
implementation are the team's own work. Third-party libraries are credited under
[Dependencies](#dependencies). AI coding assistants were used during development,
consistent with the competition rulebook.
