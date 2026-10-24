# keypool-guard

> **Stop hunting random 400/401s. keypool-guard actively probes every key in your LLM pool, quarantines the bad ones, and brings them back the moment they recover — as a tiny daemon beside your existing gateway, not another gateway.**

A health guardian for LLM API key pools. It sends each key a periodic minimal real
request (a 1-token chat completion), classifies the failures (revoked / banned /
out of quota / rate-limited / network), quarantines the bad keys, and publishes
the survivors as a healthy subpool your existing gateway can consume.

```
   keys              keypool-guard (a tiny sidecar, NOT another gateway)
   ────              ──────────────────────────────────────────────────
   key 1 ─┐         ┌────────────────────────────────────────────────┐
   key 2 ─┼──────▶  │ probe every interval (async, bounded lanes)    │
   key 3 ─┤         │   ├─ 401 / 403 / 400-quota ─▶ quarantine (red) │
   key 4 ─┘         │   ├─ 429 / 5xx / timeout ───▶ flagged, retry   │
                    │   └─ 200 ──────────────────▶ healthy (green)   │
                    │                                                │
                    │ quarantine ──N consecutive 200s──▶ re-admit    │
                    └──────────────────┬─────────────────────────────┘
                                       │  keypool.healthy.json  +  GET /healthy
                                       ▼
                    your existing gateway (litellm / one-api / gpt-load / your code)
                                       │
                                       ▼
                              users see stable 200s
```

- **Proactive, not reactive.** Gateways typically drop a key *after* it fails on
  real traffic — which means your users are the probe. keypool-guard probes idle
  keys on a schedule so a revoked key leaves the pool before it eats a request.
- **Recovery with hysteresis.** A quarantined key is re-admitted only after N
  *consecutive* healthy probes (configurable). One lucky 200 doesn't flip the
  pool back and forth.
- **Beside, not instead.** No traffic flows through keypool-guard. Keep your
  gateway; feed it the healthy subpool.

## Quickstart

```bash
# 1. Clone and install (Python 3.9+, only httpx + PyYAML)
git clone https://github.com/Jackzigen/keypool-guard && cd keypool-guard
pip install .

# 2. Generate an example config and point it at your providers
keypool-guard init > pool.yaml

# 3. Edit pool.yaml: providers, keys (use ${ENV_VAR} refs, not literals), model

# 4. One-shot report — probe every key once, print the table, exit
keypool-guard check -c pool.yaml

# 5. Or run as a daemon: publish the healthy subpool on a schedule
keypool-guard daemon -c pool.yaml
```

Exit codes for `check`: `0` all keys healthy, `1` some quarantined, `2` config
error — so cron/CI can react to the report without parsing it.

## What a probe looks like

Every `interval_seconds`, each key gets exactly one minimal real request:

```json
POST {base_url}/chat/completions
Authorization: Bearer <key>
{"model": "<cheap model>", "messages": [{"role": "user", "content": "ping"}],
 "max_tokens": 1, "stream": false}
```

Failures are classified so the report tells you *why* a key is out:

| Result | Classification | Quarantined? |
| --- | --- | --- |
| `200` | healthy | — |
| `401` | invalid (revoked / typo) | yes |
| `403` | forbidden (banned) | yes |
| `400` / `402` | insufficient quota (also 429 with an `insufficient_quota` body) | yes |
| `429` | rate-limited (transient) | no — flagged, retried next round |
| `500` / `503` | server error (transient) | no — flagged, retried next round |
| timeout / connect error | network error (transient) | no — flagged, retried next round |

Hard-fail classes (invalid / forbidden / insufficient quota) quarantine a key
immediately. Transient classes never do — a burst of 429s shouldn't empty your
pool.

### Hysteresis: back in only after N consecutive healthy probes

A quarantined key must pass `restore_after_successes` *consecutive* probes to
re-enter the healthy pool. Any failure resets the streak; readmission resets it
again. This is the anti-flapping guard: keys that oscillate at the provider
boundary stay out until they are consistently back.

## Two ways to use it

**① One-shot report** — for humans, cron and CI:

```
$ keypool-guard check -c pool.yaml
PROVIDER      KEY                    STATUS             LATENCY(ms) FAILS DETAIL
------------  ---------------------  -----------------  ----------  -----  ----------------------------
mock          sk-abc...1234          HEALTHY            142ms       0     checked 2026-10-24 06:00:00
mock          sk-def...5678          INVALID            99ms        3     revoked: unknown api key

healthy 1/2 keys across 1 provider(s)
```

Raw key values are never printed — only masked labels.

**② Daemon** — for gateways. `keypool-guard daemon` writes the healthy subpool
every round to `keypool.healthy.json` (atomic replace, `0600` permissions) and
serves it at `GET /healthy` (default `127.0.0.1:8390`; `GET /healthz` for
liveness, `503` before the first round completes). Point your gateway's key list
at that file — e.g. a config loader that reads `providers[].keys` from
`keypool.healthy.json` instead of a static list.

```json
{
  "updated_at": "2026-10-24T06:00:00Z",
  "providers": [
    {"name": "openai", "base_url": "https://api.openai.com/v1",
     "model": "gpt-4o-mini", "healthy": 3, "total": 5,
     "keys": ["sk-live-key-1", "sk-live-key-2", "sk-live-key-3"]}
  ]
}
```

Note: this file and endpoint necessarily contain **raw key values** — that is
the point (your gateway needs them). Bind the endpoint to localhost, protect
the file; both are `127.0.0.1` / `0600` by default. Everywhere else (reports,
state file, logs) keys are masked.

State (streaks, quarantine status) persists across restarts in
`keypool_state.json`, so a daemon restart doesn't reset hysteresis.

## Configuration

```yaml
interval_seconds: 60          # probe cadence
state_file: keypool_state.json
output_file: keypool.healthy.json

server:                       # optional HTTP endpoint for the healthy subpool
  enabled: true
  host: 127.0.0.1
  port: 8390

providers:
  - name: openai
    base_url: https://api.openai.com/v1   # OpenAI-compatible endpoint
    model: gpt-4o-mini                    # cheap model for the 1-token probe
    probe:
      timeout_seconds: 15
      max_concurrency: 8        # per-provider in-flight probe cap
      restore_after_successes: 3  # N consecutive healthy probes to re-admit
    keys:
      - value: ${OPENAI_KEY_1}  # expanded from the environment
      - value: sk-plain-key
        name: backup            # optional label shown in reports
```

`${VAR}` references are expanded from the environment so keys never have to sit
in the YAML file itself. Run `keypool-guard init` to print this example.

## How it compares on active probing

The reason keypool-guard exists: gateways are good at *reacting* to failed
requests, but probing keys *before* real traffic hits them is at best a
secondary feature — and probe-gated re-admission with configurable hysteresis
is rarer still.

| Dimension | keypool-guard | gpt-load | litellm router | one-api |
| --- | --- | --- | --- | --- |
| What it is | probe daemon beside your gateway | full gateway (Go + DB + web UI) | full gateway / SDK router | full gateway + web console |
| Scheduled probing of *idle* keys | ✅ core feature, every `interval_seconds` | ✅ built-in background key validation (`key_validation_interval_minutes`, default 60) | ⚠️ cooldown is failure-driven; scheduled background health checks exist as **opt-in** proxy config (`background_health_checks`, 300 s when enabled) | ❓ channel management is admin-UI-centric; no first-class scheduled per-key prober documented |
| Failure classification granularity | 401 / 403 / 400-quota / 429 / 5xx / timeout, transient vs. hard-fail | cumulative failures → blacklist (`blacklist_threshold`, default 3) | 429 immediate; >50% failures/min; 401/404/408 non-retryable | — |
| Re-admission policy | **probe-gated hysteresis**: N consecutive healthy probes, any failure resets the streak, N configurable | automatic recovery after blacklist (interval-based; not documented as N-success hysteresis) | **fixed timer** (5 s default) — "Deployments automatically recover from cooldown after the cooldown period expires", not probe-gated | — |
| Consumable by *other* gateways | ✅ its whole job: JSON file + HTTP endpoint | it *is* the gateway | it *is* the gateway | it *is* the gateway |

Evidence for the litellm column (checked against
[docs.litellm.ai/docs/routing](https://docs.litellm.ai/docs/routing) and
[docs.litellm.ai/docs/proxy/health](https://docs.litellm.ai/docs/proxy/health),
October 2026):

- Cooldowns trigger on *real-traffic* failures: "The router automatically cools
  down deployments based on the following conditions" — Rate Limiting (429,
  immediate), High Failure Rate (>50% failures in the current minute),
  Non-Retryable Errors (401/404/408); each with "5 seconds (default)" cooldown.
- Recovery is a timer, not a probe gate: "Deployments automatically recover
  from cooldown after the cooldown period expires."
- Background health checks are opt-in: "By default `/health` probes every model
  on each call. To avoid querying models too frequently, run the checks in the
  background" via `background_health_checks: true` / `health_check_interval:
  300`.

The gpt-load row reflects its
[README](https://github.com/tbphp/gpt-load) configuration reference at the time
of writing (it genuinely does validate keys in the background — the difference
is that this ships inside a full Go gateway with a database and web console,
while keypool-guard is a single sidecar whose only job is pool health). The
one-api row is marked ❓ deliberately: we did not re-verify its current behavior
for this table; treat it as "not its documented focus" and check its docs.

## Design notes

- **Minimal real requests.** Classification by HTTP status + error body, using
  providers' own semantics (including the `insufficient_quota` error code that
  rides on 429). A 1-token probe costs effectively nothing; a false-healthy key
  costs a user request.
- **Bounded concurrency.** Probes within a provider run under a semaphore
  (`max_concurrency`) so a 500-key pool doesn't DDoS its own provider — or trip
  the very rate limits it is measuring.
- **Zero-heavy dependencies.** `httpx` + `PyYAML` only. State and the healthy
  subpool are plain JSON; the HTTP endpoint is stdlib. No database, no Docker,
  no admin UI.
- **Failure to save is failure to run.** State writes are atomic
  (tmp + fsync + rename) so a crash mid-write can't corrupt hysteresis counts.

## Development

```bash
pip install -e .[dev]
python -m pytest          # or: python run_tests.py
```

The test suite exercises the probe / quarantine / backfill path against a real
local HTTP mock server (stdlib `ThreadingHTTPServer` speaking an
OpenAI-compatible dialect) — real sockets, real status codes, real timeouts.
No `patch` stubbing on the probing path.

## License

[MIT](LICENSE) © 2026 Jackzigen
