# NOTES — the user's world (Unisense project)

Raw notes on the tools, channels, and terminology of this project, so recurring
workflows can be specified in the user's own vocabulary. Fuzzy terms are sharpened
into canonical ones as they surface.

## Project shape
- **Unisense** — metrics / semantic-layer platform. Repo: `git@github.com:lcp5674/Unisense.git`.
- Monorepo-ish: `backend/` (FastAPI + SQLAlchemy async + MySQL), `frontend/`, `spec/`, `docs/`, `workflows/`, `scripts/`.
- Backend entry / lifespan wires the degradation listener (`handle_circuit_signal`) and seeds `dependency_health`.

## Gates / toolchain (the shared quality bar)
- `mypy --strict app/` must be **0 errors** (currently green: 175 source files).
- `ruff check` + `ruff format --check backend` (format is a hard CI gate).
- `pytest tests/unit/...` per area.
- Commit-msg hook `scripts/check_commit_msg.py`: enforces `^[service] action: brief (TD§x.y)?$`.
  - 14 valid service prefixes (one is `consume` — FR-17's owning path).
- `docs/module-status.yaml` (canonical module status); FR-17 key = `fr17_capability_degradation`.
- Spec authority: `TD` (technical design doc) §4.13 / §5.2 / §11; `DEV_GUIDE` §6.3 / §17.

## Canonical terminology (fuzzy → canonical)
- 降级 / degradation → **capability degradation** (the module under FR-17 review).
- 熔断 / circuit breaker → `CircuitBreaker` in `app/core/resilience.py`.
- 实时健康态 / dependency_health → real-time health snapshot table (dashboard source).
- 审计事件 / degradation_event → WORM audit event table (only-written, never deleted).
- 探针 / probe → health probe (`optional_dependency_status` TCP liveness, or ES `.ping()`).
- 半开 / half-open → breaker half-open single-flight probe window.
- 占位 / placeholder → stub / `TODO` / `pass`-only / `NotImplementedError` (the review scans for these).
- 去重 / dedup → `_fired_state` suppression of repeat same-state events.
- 遥测 / telemetry → `latency_p95_ms` / `error_rate_pct` / `meta` on `dependency_health`.

## FR-17 review — running questions (to grill)
- **Worker topology:** how many uvicorn workers / replicas? (drives gap D — cross-process dedup).
- **`dependency_id` cardinality:** are ids dynamic per-datasource / user-derived, or fixed (olap/es/graph)? (drives gaps A & B — unbounded caches).
- **Probe call site:** is `optional_dependency_status()` ever called on the event loop (health endpoint)? (drives gap C — blocking socket).
- **CI trigger:** is there a hook to auto-run this loop on FR-17 file changes, or is it manual/nightly only?

---
