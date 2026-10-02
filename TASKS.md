# Tokendrain implementation task log

## Objective
Deliver the NixOS-hosted daemon, persistent isolated Firecracker projects, Codex execution,
credential brokers, web interface, schedules, reports, and tested recovery paths.

## Work in progress
- [ ] Platform: flake, NixOS module, reproducible microvm.nix guest, privileged helper.
- [ ] Host: relational migrations, API, project/run state machines, scheduling, SSE.
- [ ] Execution: persistent disks, exclusive leases, snapshots, isolated networking.
- [ ] Codex: typed RPC, guest protocol, unsupervised loop, usage predicates, reports.
- [ ] Credentials: current SIWC flow, import compatibility, refresh locking, GitHub Apps.
- [ ] UI: dashboard, projects, runs, schedules, settings, integration and storage controls.
- [ ] Validation: Python unit/integration checks, frontend build, Nix evaluation/build.
- [ ] Documentation: installation, operations, threat model and real KVM test procedure.

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
