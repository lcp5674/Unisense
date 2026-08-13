---
title: FR-17 Capability Degradation — Industrial-Grade Review Loop

summary: |
  Recurring review-and-harden loop for the capability-degradation module
  (app/core/degradation.py, app/core/resilience.py and their tests). Keeps the
  module at industrial grade: no placeholders, bounded resources, non-blocking
  I/O, accurate observability, and real fault-tolerance under production
  scenarios (multi-worker, degradation storms, dynamic dependency ids,
  event-loop-sensitive probes).

kind: workflow

scope: resilience

# Loop specification

## Loop pattern
Recurring industrial-grade review of the FR-17 capability-degradation module.

**Why this loop exists:**
- The module is the system's safety net when optional dependencies
  (OLAP / ES / Neo4j / LLM / DATASOURCE / NOTIFICATION) degrade. The degradation
  path must itself never degrade — every failure inside it is best-effort and
  must not take down the caller.
- Prior passes already hardened it (event dedup, ENUM boundary checks,
  `_MISSING`-sentinel telemetry preservation, half-open single-flight,
  lost-probe self-heal). This loop prevents regressions and catches the *next*
  class of issues that unit tests alone don't reveal.
- Production scenarios expose gaps invisible in unit tests: multi-worker
  duplicate events, unbounded in-memory caches, event-loop-blocking probes,
  connection-pool storms, monotonic→wall-clock drift.

**Where this loop appears:**
- On any change to `app/core/degradation.py`, `app/core/resilience.py`, or their tests (event trigger).
- Nightly safety-net (schedule) — catches slow drift / new dependency types.
- Incident-triggered reassessment after any degradation-related production incident.

## Workflow steps (one run = one review pass)

### Step 1: Line-by-line re-read + placeholder scan (automated + manual)
For every line of the two modules and their call sites:
- Flag any stub / `TODO` / `pass`-only / `raise NotImplementedError` / hardcoded placeholder value.
- Confirm each function implements *real* business logic, not a no-op shell.
- Verify call sites (`es_client.py`, `consume/service.py`, `api/health.py`, EventBus wiring) actually invoke the hardened paths.

### Step 2: Production-scenario fault analysis (checklist, manual)
Walk concrete production scenarios and verify behavior:
- **Multi-worker:** dedup + shared state correctness across processes.
- **Dynamic dependency ids:** cardinality of `dependency_id` (per-datasource? user-derived?) and its effect on any in-memory caches.
- **Degradation storm:** many deps failing at once → DB session / connection-pool pressure.
- **Event-loop sensitivity:** any synchronous blocking I/O on the asyncio loop.
- **Observability accuracy:** timestamps reflect real event times (MTTR), not record times.

### Step 3: Static + dynamic gates (automated)
Run the project's gates and require green before any fix lands:
- `mypy --strict app/core/degradation.py app/core/resilience.py` → 0 errors.
- `ruff check` + `ruff format --check` on changed files.
- `pytest tests/unit/test_degradation.py test_dependency_health.py test_resilience.py` → pass.
- `python3 scripts/check_commit_msg.py` on the candidate message (enforces `[service] action: brief (TD§x.y)?`).

### Step 4: Fix with evidence (automated, gated)
Each gap found in Step 2 becomes:
- a minimal, backward-compatible fix, **and**
- a new unit test that fails without the fix (regression lock), **and**
- a re-run of the Step 3 gates proving green.
No fix lands without its test + gate proof.

### Step 5: Human checkpoint (manual) — see Checkpoint section
Present a **brief** (not raw diffs): what was found, what was fixed, gate evidence, risk. User approves the fix batch.

### Step 6: Finalize (automated)
- Commit per `[consume]` convention with `TD§` refs (FR-17 owns the consume path).
- Update `module-status.yaml` evidence for `fr17_capability_degradation` (perf baseline / gate proof).
- Optionally open a PR (user decision).

## Known gaps (captured 2026-08-13 review — to be closed in runs)

| ID | Location | Issue (production scenario) | Severity | Fix |
|----|----------|----------------------------|----------|-----|
| A | `degradation.py:58` `_fired_state` | Unbounded module-level dict keyed by `(dependency_type, dependency_id)`; never evicted. Under dynamic `dependency_id` (per-datasource) → **memory leak** over process lifetime. | High | Bounded LRU/TTL eviction. |
| B | `resilience.py:253` `_unknown_breakers` | Same class: `get_circuit_breaker` lazily caches breakers for arbitrary service names, never evicted → **memory leak** if service names are dynamic. | High | Bounded cache / cap. |
| C | `resilience.py:194-226` `optional_dependency_status()` | Uses blocking `socket.create_connection` (0.5s timeout × 3 deps ≈ 1.5s). If invoked on the asyncio loop (health endpoint / probe) it **freezes the whole event loop**. | High | Run probe via `asyncio.to_thread` / async socket. |
| D | `degradation.py:58,391` dedup | `_fired_state` is per-process. Multi-worker deploys each dedup independently → duplicate same-state events still hit the WORM `degradation_event` table across workers. | Medium | Document; shared store only if audit volume warrants. |
| E | `degradation.py:82` `_signal_to_health_params` | `circuit_opened_at = datetime.now(UTC)` is *record* time, not breaker *open* time (signal carries `opened_at` as `time.monotonic`, not wall-clock). MTTR dashboards read a wrong "down since". | Medium | Capture wall-clock at breaker open. |
| F | `degradation.py:_schedule_persist` | Each fire-and-forget event opens its own `async_session_factory()` session; under a storm this spawns many concurrent sessions (pool pressure). Dedup + low frequency mitigate. | Low | Optional bounded semaphore. |

Gaps already closed in prior passes (regression-locked by tests): event dedup (#D partially),
ENUM boundary validation (`DEGRADATION_STATES` / `DEP_HEALTH_STATES` / `DEP_HEALTH_CIRCUIT`),
`_MISSING`-sentinel telemetry preservation on UPSERT, half-open single-flight,
lost-probe self-heal (`probe_timeout`), instance-level `dependency_id`.

## Details

### Trigger specification
**Proposed (grilling-resolved — see Q1):** event-triggered on any change to the
two module files or their tests, plus a nightly schedule as a safety net.
(Spec deliberately event-driven: a change to the safety-net module is exactly
when a review must run; the nightly pass catches slow drift.)

### Checkpoint (grilling-resolved)
Proposed: Step 5 human checkpoint — a **brief** of findings + fix batch + gate
evidence; user approves before commit. No checkpoint inside the automated gates.

### Push right principle
- Steps 1-4 do maximal automated work (re-read, scenario analysis, gates, fixes + tests) before involving the human.
- The checkpoint brief presents decision-ready content: *what was found, what was fixed, gate proof, risk* — never the raw diff or draft.
- User reviews once, late, with everything prepared.

### No AI mandate
Human approves every fix batch. Automation runs the gates and applies
test-gated fixes; it does not decide scope or ship unilaterally.

### Dependencies
- Toolchain: `mypy --strict`, `ruff`, `pytest`, `scripts/check_commit_msg.py`.
- Status: `module-status.yaml` (key `fr17_capability_degradation`), 14-service `[prefix]` rule.
- Specs: TD §4.13 / §5.2 / §11, DEV_GUIDE §6.3 / §17.

---
