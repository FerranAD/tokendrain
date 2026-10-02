# Tokendrain implementation task log

## Objective
Deliver the NixOS-hosted daemon, persistent isolated Firecracker projects, Codex execution,
credential brokers, web interface, schedules, reports, and tested recovery paths.

## Implementation checklist
- [x] Platform: flake, NixOS module, reproducible microvm.nix guest, privileged helper.
- [x] Host: relational migrations, API, project/run state machines, scheduling, SSE.
- [x] Execution: persistent disks, exclusive leases, snapshots, isolated networking.
- [x] Codex: typed RPC, guest protocol, unsupervised loop, usage predicates, reports.
- [x] Credentials: current SIWC flow, import compatibility, refresh locking, GitHub Apps.
- [x] UI: dashboard, projects, runs, schedules, settings, integration and storage controls.
- [x] Validation: Python unit/integration checks, frontend build, Nix evaluation/build.
- [x] Documentation: installation, operations, threat model and real KVM test procedure.

## Evidence and decisions
- 2026-10-02: Repository initially contains only a README. Nix available; Python/Node
  will be supplied through Nix. /dev/kvm and /dev/net/tun absent in this environment.
- Current official OpenAI documentation describes dynamic open-source SIWC registration,
  Responses-provider app-server configuration and restart/resume for token renewal.
  Authentication implementation follows these sources rather than inventing OAuth endpoints.
- Privileged VM/network work belongs to a narrowly defined local helper; HTTP daemon is
  an ordinary service user. Runtime credentials go through vsock and tmpfs only.
- Parallel component notes live under docs/task-*.md until final integration.

## Paused checkpoint — user requested Codex restart

2026-10-02: User requested a graceful stop so the next Codex session can pick up host
KVM/TUN changes. Last direct checks still returned ENOENT for /dev/kvm and /dev/net/tun
inside this sandbox. **Do not interpret this checkpoint as project completion.**

Current checks: full existing pytest suite 24 passed; Ruff passes across src/tests;
strict mypy passes across 43 source files. These cover implemented infrastructure,
credentials, protocol and guest/Codex tests; host services/scheduler/orchestrator have
not yet received their required unit/integration suite. Guest Nix derivation evaluated;
real package build, NixOS module boot and KVM tests remain.

Resume from [docs/RESUME.md](docs/RESUME.md), plus component task logs. Recheck devices
first, then complete host/API/CLI and frontend entrypoint, integrate, test and document.

Checkpoint commit attempted but `.git/index.lock` creation failed: `.git` is mounted
read-only in this session. All source and handoff files are saved in the working tree;
no commit was created. Resume session can commit if Git metadata becomes writable.

## Resumed implementation milestones

- KVM API 12 and CREATE_VM work; TUN accessible in restarted session. Git writes work.
- Committed domain/migration/supervisor foundations as 7704452.
- Guest/Auth committed with current SIWC, Codex protocol and token refresh tests.
  Actual installed Codex smoke caught/fixed thread sandbox enum spelling.
- Frontend complete: strict TypeScript, Vite build and nine Chromium tests pass.
- Real Firecracker tests pass: fresh boots, vsock, persistent workspace/home/Nix state.
- Nested NixOS tests pass for two concurrent VMs, privileged helper, public egress,
  host/private/public-LAN/interguest blocking, firewall reloads, route changes,
  crash reconciliation and graceful shutdown. No privileged host configuration modified.
- Host API implemented: session authentication, CSRF/Host protection, CRUD, runs,
  schedules, storage, secrets, OpenAI and GitHub setup, system settings, replayable SSE.
- Ten new host integration tests pass (API, migrations, scoped GitHub token locking).
- Full NixOS module HTTP smoke passes: packaged UI, authenticated CLI/API, migrations,
  helper readiness, runtime credential permissions and clean service stop.
- At this intermediate milestone, supervisor recovery tests, the live browser flow,
  security review and final quality gates were still pending; all completed below.
  Live OAuth/inference/GitHub verification remains external; implementation-session
  credentials have never been reused.

## Final integration review

- Actual browser against the daemon verified project/task/feedback/secrets/snapshot/restore/resize,
  run reports and SSE, history, scheduling and settings. Eleven Chromium tests pass.
- Provider sign-in host identity now uses the officially required stable UUID URI format.
- Project deletion and Run admission share SQLite writer transactions to prevent deletion races.
- Reviewed credential cancellation durability, shutdown failure reporting, database-failure cleanup,
  notification backpressure and automatic daemon restart on background failure. Fixes and regression
  tests are committed. Generic secret edits serialize with Run admission and preserve committed
  references when a database commit is interrupted.

## Final verification — 2026-10-02

- Local Python suite: **119 passed**, with the opt-in KVM test selected separately.
- Ruff lint and formatting clean; strict mypy passes across **57 source files**.
- TypeScript, Prettier and Vite production build pass. **11 Chromium tests pass**,
  including the actual HTTP daemon in explicit mock mode, reports and live events.
- Nix builds of `tokendrain`, `tokendrain-guestd` and `guest-artifacts` pass.
  The sandboxed Python package build runs **118 passed, 1 skipped** (local Codex
  executable smoke unavailable in the build sandbox), with KVM deselected.
- `nix flake check --no-build --all-systems` passes for x86_64-linux/aarch64-linux.
  Actual guest and NixOS tests were executed on x86_64-linux.
- Rebuilt direct Firecracker test: **1 passed in 18.36 seconds**, confirming fresh
  boots, vsock/Codex control, workspace/home/Nix-state persistence, and tmpfs cleanup.
- Rebuilt NixOS service test: **passed in 18.61 seconds**, including packaged UI,
  authenticated CLI/API, migrations, key permissions and clean shutdown.
- Rebuilt nested Firecracker/helper/network test: **passed in 133.50 seconds**,
  including simultaneous guests, public egress, host/LAN/interguest isolation,
  firewall reloads, route changes, helper recovery and orderly disk release.
- Temporary browser-test daemon and VM tests stopped; no host firewall or NixOS
  configuration was changed. Implementation and documentation are committed locally.

### Remaining external acceptance

Real user-account SIWC/import sign-in, actual model inference, multi-hour provider
token renewal and GitHub branch/PR writes require credentials configured by the
administrator through tokendrain. These are implemented and covered with fakes but
are not claimed as live-tested. See [the acceptance checklist](docs/development.md#live-account-acceptance).
Installation starts with [README.md](README.md); no runtime secrets belong in Git or
the Nix store. Parallel implementation workers reached their provider usage limit
during finalization; the main session completed all remaining local checks.

## Installation fix — IPv4 forwarding composition

- User installation exposed duplicate `net.ipv4.ip_forward = 1` definitions from
  Tokendrain and a VPN module. NixOS sysctl values use `mergeOneOption`, rejecting
  even equal ordinary definitions.
- Tokendrain now sets `lib.mkDefault 1` and asserts that the effective value stays
  enabled. Existing VPN/router definitions compose without forcing their priority.
- The NixOS service smoke test includes an external ordinary forwarding definition
  and checks the running kernel value. Rebuilt test passed in **18.82 seconds**.
- Seven focused evaluations passed: standalone, external `1`/`"1"`/`true`, and
  expected assertion failures for `0`/`false`/`null`.
- Flake evaluation passes for both supported architectures; Ruff and strict mypy
  remain clean. Networking composition behavior is documented in `docs/microvms.md`.
