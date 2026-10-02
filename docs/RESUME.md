# Resume checkpoint

The user asked to stop gracefully and restart Codex so KVM/TUN may become accessible.
Continue the original full implementation request. Do not settle for the checkpoint.
Do not ask for library or architecture choices. The user authorized Nix shells/builds.

## First actions

1. Check `/dev/kvm`, `/dev/net/tun`, groups and open KVM with API-version and create-VM
   ioctls. They were ENOENT in the old execution sandbox even after host changes.
2. Read this file, TASKS.md, docs/task-infra.md, docs/task-codex.md,
   docs/task-web.md, and web/API.md. Check git status before editing.
3. Resume host API/CLI and integrate the already-written frontend and backends.

## Local environment

Working directory `/home/ferran/tokendrain`, NixOS, Nix 2.34.8, no Python or Node
on default PATH. `.venv` exists with Python3.12 and `uv sync` dependencies.

```
XDG_CACHE_HOME=/tmp/tokendrain-cache UV_CACHE_DIR=/tmp/tokendrain-uv \
  nix shell nixpkgs#python312 nixpkgs#uv --command uv sync
LD_LIBRARY_PATH=/nix/store/604gsr59rj7dzd0nrhp143rpvf7gyiaz-gcc-15.3.0-lib/lib \
  .venv/bin/pytest -q
.venv/bin/ruff check src tests
LD_LIBRARY_PATH=/nix/store/604gsr59rj7dzd0nrhp143rpvf7gyiaz-gcc-15.3.0-lib/lib \
  .venv/bin/mypy
```

The LD_LIBRARY_PATH is needed for wheel greenlet/libstdc++ on NixOS; prefer the new
flake devShell once fully evaluated. Never overwrite HOME/CODEX_HOME. Home cache is
read-only, hence XDG_CACHE_HOME in /tmp. Nix network/build access works.

## Implemented root-owned code

- pyproject.toml + uv.lock, Python3.12, FastAPI/Pydantic/SQLAlchemy/Alembic/httpx,
  cryptography/PyJWT/croniter/python-dotenv, Ruff/mypy/pytest.
- `domain.py`: validated project/run/schedule/report shapes, explicit execution
  transition table, ANY budget predicates, provider/completion natural stops.
- `config.py`: env Settings prefix TOKENDRAIN_, used by Nix module.
- `db/models.py`: relational Project/Run/ProjectExecution/Report/UsageSnapshot/
  Schedule/Event/ProjectSnapshot/SecretEntry/GitHubApp/Installation/ProjectGitHub/Setting.
- Initial static Alembic migration generated and checked in from model metadata;
  production `migrate(path)` runs Alembic via asyncio.to_thread; no create_all.
- `services.py`: ProjectService + RunService, CRUD primitives, DB reservation
  via partial unique index for all active/queued project executions, snapshots,
  state transitions, usage observations. Methods list_projects/list_runs.
- `events.py`: persisted SSE replay using Event IDs, condition wakeups/heartbeats.
- `scheduler/service.py`: timezone-aware cron, due triggers create ordinary Runs
  and advance next occurrence in one transaction, coalesce downtime, skip overlap.
- `github/provider.py`: RS256 JWT, paginated installation/repository discovery,
  narrowed installation token caching/locking; only repo-specific permission subset
  enters guest. Uses current official GitHub API version 2026-03-10.
- `orchestration/driver.py`: GuestClient + CodexClient work session, turns with
  structured reports, credential renewal interrupts at safe acknowledgement then
  restart/resume; no blind replay, redacts completed log units, usage notifications.
- `orchestration/supervisor.py`: owned TaskGroup dispatcher, concurrency and per-run
  parallel mode, startup orphan stop/fail recovery, snapshot/lease/start/turn/stop,
  persistence, cancellation, report task-log updates preserve concurrent user edits.

## Critical incomplete work

1. **Host daemon/API not implemented**: `api/` and `cli/` have only init files.
   Need service container creation, lifespan migrations/single-daemon flock,
   HTTP client/store/auth/GitHub/storage/helper/factory construction, supervise
   scheduler+execution tasks with owned cancellation, routes implementing web/API.md.
   pyproject console scripts currently reference missing host CLI modules.
2. Admin security: planned POST /api/v1/session {token} sets HttpOnly SameSite cookie,
   DELETE session, frontend sends X-Tokendrain-Request:1 on mutations. Validate Origin
   and CSRF custom header; use generated admin token runtime file in state dir or
   configured token file, HMAC-signed session cookie, Bearer support for CLI.
   Protect API and SSE. Add TrustedHost-like enforcement to stop DNS rebinding.
   Do not expose encryption key or admin token through system API.
3. Implement all project/storage/secrets/GitHub/schedule/run/system/OpenAI endpoints.
   Storage mutations must require idle project AND hold project lease; run reservation
   race with external mutation needs careful atomic coordination. Secrets metadata in
   SQLite, values in EncryptedFileCredentialStore; dotenv parser rejects malformed
   lines and does not interpolate. Secret env variable restrictions need align guest.
4. OpenAI callback `/auth/callback` on **http://127.0.0.1:<port>** per current official
   SIWC. `begin_sign_in` returns url/state; callback calls complete_sign_in with
   provider code/state/issued client_id. Remote browser must SSH-tunnel callback.
   No invented OAuth behavior. API auth method UI wants chatgpt/import; backend
   AccountInfo method siwc/import. Account list metadata stored encrypted in files.
5. Models + current usage discovery before run: fetch official SIWC model endpoint
   or start host-side app-server in fresh tmpfs with runtime credentials, no agent
   turns on host. Imported Codex mode supports account/rateLimits/read. SIWC custom
   provider may not: fail closed on unobservable percentage predicates and explain
   duration/provider-limit fallback. Never fake zero usage.
6. Mock infrastructure/session injectable backend for full app tests and local
   demo mode (must clearly label mock). Real Firecracker backend already exists.
7. Host unit/integration tests: migrations forward/idempotent/schema parity, API CRUD,
   run creation reservation, transitions, usage normalization/predicates, scheduler
   DST and duplicate/overlap, full fake execution/cancellation/recovery/SSE, GitHub
   scoped JWT requests, secrets parser and API auth/CSRF.
8. **Review root code before relying on it**: code is typed/linted but untested in
   integration. Potential issues: RunService.create_run_in adds executions before
   run flush (verify SQLAlchemy FK ordering); startup failures before PREPARING could
   leave reservation; stopping failure intentionally retains reservation but needs
   operational retry; event data retention limits; cancellation shutdown resource
   ownership; provider status error shapes; elapsed time currently starts after boot;
   previous report needs inclusion in initial context; disconnected Codex session
   recovery currently fails preserved execution rather than bounded restart/read.
9. Complete frontend entrypoint/styles per task-web. Ensure npm build/typecheck.
10. Nix packaging/module real eval/build/tests; helper boundary validation. Update
    docs/architecture.md security.md microvms.md github-app.md development.md and
    README realistic flake install example; admin doctor/status/projects/runs CLI.
11. Review all code security isolation, run quality gates, commit coherent milestones.

## Existing validation

At graceful checkpoint: all 24 existing tests pass, full Ruff passes, strict mypy
43 source files passes, compileall passes. Infrastructure agent separately evaluated
microvm.nix guest-artifacts derivation. No live OAuth/GitHub credentials used, no
real VM boots yet. Component log files contain exact contracts and caveats.

## Collaboration ownership

Previous agents have stopped; spawn new ones to parallelize further if useful:
- infrastructure: flake.nix/lock, nix/, modules/, vm/, networking/, storage/, infra tests
- guest_codex: auth/, credentials/, codex/, protocol.py, tokendrain_guestd/, tests/docs
- frontend: web/, docs/task-web.md
Root owns host API/DB/orchestration/scheduler/GitHub/CLI and global integration.

Git checkpoint note: `git add` failed with `.git/index.lock: Read-only file system`.
All implementation files remain untracked in working tree. No commit created; do not
mistake clean `git diff` for absence of code (use `git status --short` / rg --files).
