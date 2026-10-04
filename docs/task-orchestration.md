# Orchestration reliability milestone

Implemented and tested the host execution loop against injected storage, VM,
Codex transport and credential providers without using external accounts.

- Run elapsed budgets start at the persisted Run start time, cover preparation
  and boot, and are shared by sequential project executions. An already-expired
  Run stops before booting another VM. Usage and elapsed rules stop at completed
  useful-turn boundaries; user cancellation interrupts an active turn explicitly.
- Missing usage windows fail closed before any work when percentage rules cannot
  be evaluated. Provider exhaustion remains a normal stop; infrastructure failure
  never becomes successful completion.
- The initial prompt includes the previous report and durable project state.
  Feedback is consumed only after a useful turn returns. Concurrent user edits
  to Backlog approval remain enforced in host task mutations.
- Reports use an explicit strict Structured Outputs schema with all fields
  required and no arbitrary metadata dictionaries. Usage observations remain
  host-owned. Malformed agent output is retained as an uncertain in-progress
  report, never interpreted as completion.
- Real sessions pin a connected account, redact structured values recursively,
  and handle process-exit/failure events. Up to two session recoveries reconnect,
  read persisted thread state, and resume without replaying the interrupted
  side-effecting turn. Further progress uses a fresh inspect-state instruction.
- Credential renewal interrupts once, waits for acknowledgement, rotates and
  resumes the same thread. Unexpected process failures close the guest channel.
  Guest connection acceptance waits until the old session's cleanup finishes.
- Partial VM start failures reconcile resources for that execution. Unconfirmed
  teardown retains the database reservation until startup reconciliation stops
  the VM. Reconciliation updates reports to failed while preserving useful work.
- Codex notification queues have both count and byte bounds, and accumulated turn
  report text has an 8 MiB bound. Redaction covers raw and JSON-escaped secrets.
- Global concurrency and Run parallelism are enforced by the owned TaskGroup.
  Tests exercise user and owner cancellation with resource cleanup.
- Cron calendar candidates are checked using timezone roundtrips. Nonexistent
  spring-forward times are skipped; repeated autumn times run at both distinct
  UTC instants. A real croniter aware-time matching defect was caught and avoided.
  Invalid IANA zones become ordinary validation errors.

Validation: 55 focused unit/integration tests pass across domain/usage/scheduling,
real session protocol fakes, supervisor, authentication and guest lifecycle;
Ruff passes and strict mypy reports no issues in 16 owned modules.

Real infrastructure validation is tracked separately in task-infra.md. Live
OpenAI authorization/entitlements/refresh still require a configured account;
the implementation agent's account has not been reused.

## Teardown audit

Guest lifecycle errors now propagate while transport closure remains guaranteed.
A failed guest stop cannot become a successful execution. STOPPING persistence
and event logging are best-effort before cleanup: even a full database cannot
prevent guest cleanup or host VM stop. Terminal persistence happens only after
resource teardown is confirmed; unresolved database failure retains the existing
reservation for startup reconciliation. Regression tests inject a STOPPING write
failure and a guest shutdown failure, verifying cleanup and failed results.

Audit validation: 53 credential, protocol, session, supervisor, guest lifecycle and
account-probe tests pass; Ruff is clean and mypy passes all 11 affected modules.
