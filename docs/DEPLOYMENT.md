# Deployment Guide

How to get the service onto a public URL the judge can reach, plus the required
Docker fallback image.

---

## Requirements to satisfy

| Requirement | Source | Why it matters |
|---|---|---|
| Public base URL answering `GET /health` and `POST /optimize-energy` | Problem Statement §06 | 3 pts — live endpoint reachability |
| No login, VPN, or dashboard approval on the judging path | Participant Guide §03 | Judge cannot authenticate |
| `/health` returns `ok` within 60s of start | Guide §08 | Readiness gate before hidden tests |
| p95 latency ≤ 5s | Guide §08 | 3 pts — buys the full latency score |
| Pullable Docker image, exact tag/digest | Guide §02 | 4 pts — fallback artifact |
| Image reaches `/health` with the documented command | Guide §07 | Checked directly |
| No secrets baked into the image or repo | Guide §04 | Security violation if broken |
| Stays reachable for the whole evaluation window | Guide §03 | Intermittent availability loses cases |

---

## The cold-start trap

This is the single most likely way to lose easy points.

Free tiers on Render, Railway and Hugging Face Spaces **spin the service down
after a period of inactivity**. The next request pays a cold-start penalty of
roughly 30–60 seconds.

That breaks two rules at once:

- `/health` must answer within **60 seconds of start** — a cold start can exceed it
- p95 latency must be **≤ 5 seconds** — one cold start in a short judge run can
  wreck the 95th percentile outright

**Mitigation, in order of preference:**

1. Use an always-on tier for the judging window (the cheapest paid tier is
   typically a few dollars).
2. If you must use a free tier, keep the service warm with an external pinger
   hitting `/health` every 5 minutes.
3. Treat the Docker image as the real fallback — the guide explicitly allows it
   when the hosted endpoint is unavailable.

---

## Option A — Container host (recommended)

Works on Render, Railway, Fly.io, or any VPS. One `Dockerfile` serves both the
live endpoint and the required fallback artifact.

```bash
# 1. Build
docker build -t <dockerhub-user>/gridwise-llm:1.0.0 .

# 2. Smoke-test locally first — never push an untested image
docker run --rm -p 8000:8000 -e LLM_API_KEY=<key> <dockerhub-user>/gridwise-llm:1.0.0
curl -s http://localhost:8000/health      # {"status":"ok"}

# 3. Push
docker login
docker push <dockerhub-user>/gridwise-llm:1.0.0

# 4. Deploy on the host, pointing at that image
#    Set environment variables LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_API_STYLE
#    Expose port 8000
```

The image is already published at `jnrchayan/gridwise-llm:1.0.0`, so step 3 is
done — this is the reference the submission points at.

The image must stay **pullable for the whole evaluation window** — do not
delete the tag.

## Option B — Platform build from repository

Connect the repository and let the host build the `Dockerfile` directly. Set
the same three environment variables. Fine, but you still need a registry image
for the fallback requirement, so Option A is usually less total work.

---

## Environment variables on the host

Set these in the host's dashboard, never in the repository:

```
LLM_API_KEY=<secret>
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_STYLE=openai
```

`LLM_API_STYLE` is not optional in practice: leave it out and the service falls
back to the Anthropic request shape and sends your key to the wrong provider.
Optional extras: `LLM_TIMEOUT_SECONDS`, `LLM_MAX_TOKENS`, `LOG_LEVEL`.

All four of these must be set. A *partial* configuration — the key present but
the route variables absent — is the failure mode described in the README's
**Known limitations**: the service silently degrades every note to `no_op` while
still returning HTTP 200.

---

## Post-deployment verification

Run all of these **from outside** your development machine, as the guide
requires.

```bash
BASE=https://<your-service-url>

# 1. Health
curl -s $BASE/health
# expect {"status":"ok"}

# 2. Time the main endpoint (must be well under 5s)
time curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/optimize-energy \
  -H "Content-Type: application/json" \
  -d @tools/sample_request.json

# 3. Full end-to-end validation against the public pack
python tools/run_public_cases.py --live --url $BASE

# 4. Error handling — must not 500 or leak anything
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/optimize-energy \
  -H "Content-Type: application/json" -d '{bad json'
# expect 400

# 5. Docker fallback from a clean machine
docker pull <dockerhub-user>/gridwise-llm:1.0.0
docker run --rm -p 8000:8000 -e LLM_API_KEY=<key> <dockerhub-user>/gridwise-llm:1.0.0
curl -s http://localhost:8000/health
```

---

## Deployment checklist

- [ ] `/health` reachable publicly, returns `{"status":"ok"}`
- [ ] `POST /optimize-energy` reachable publicly, accepts the exact schema
- [ ] p95 latency under 5s, no request over 30s
- [ ] Health responds within 60s of a cold start
- [ ] Malformed JSON returns 400; invalid structure returns 422
- [ ] Provider failure still returns a valid 200 with a valid schedule
- [ ] No secret values in the repo, image, logs, or API responses
- [ ] Docker image pushed with an exact tag and pullable
- [ ] Documented `docker run` command reaches `/health`
- [ ] Service remains up for the whole evaluation window
- [ ] Repository public after the submission deadline

---

## Failure modes to watch

| Symptom | Likely cause | Fix |
|---|---|---|
| First request takes ~45s | Free-tier cold start | Always-on tier, or external keep-warm pinger |
| All notes return `no_op` | `LLM_API_KEY` missing or rejected | Check host env vars and provider quota |
| 401/403 from provider | Bad or expired key, or wrong `LLM_BASE_URL` | Verify credentials against the provider's `/v1/models` |
| 429 from provider | Rate limit or quota exhausted | Raise quota, or lower request volume during the run |
| `/health` never ready | Port mismatch | Service must bind `0.0.0.0` and the host must target the exposed port |
| Intermittent 500s | Provider timeouts | Already handled — the service degrades to `no_op` rather than failing |
