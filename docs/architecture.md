# Architecture

Tokendrain is a single-host service. Nix supplies the platform; the running application creates and manages projects. The host owns scheduling and long-lived credentials. Every execution starts the selected project’s persistent development VM with the current read-only Tokendrain control bundle.

## Components and boundaries

```mermaid
flowchart LR
    Browser[Browser UI] -->|HTTP API + SSE| Daemon[tokendraind]
    Daemon --> DB[(SQLite + Alembic)]
    Daemon --> Vault[Encrypted host credentials]
    Daemon --> Storage[Project storage backend]
    Daemon -->|restricted Unix socket API| Helper[tokendrain-helper]
    Helper -->|transient systemd unit| VMM[Firecracker]
    Helper --> Firewall[TAP + nftables]
    Daemon -->|framed JSON over vsock| Guest[tokendrain-guestd]
    VMM --> Guest
    Guest -->|stdio JSON-RPC| Codex[Codex app-server]
    Codex --> Machine[(Persistent project VM)]
    Control[Current read-only control bundle] --> Guest
    Guest --> Runtime[Runtime credentials in tmpfs]
```

| Component | Responsibilities | Authority |
| --- | --- | --- |
| `tokendraind` | API, UI, projects, runs, scheduling, account refresh, GitHub broker, reports and events | Unprivileged service user; can decrypt application credentials |
| `tokendrain-helper` | Bounded VM start/stop/list, read-only diagnostics, TAP policy, transient units, recovery records | Restricted privileged service with fixed operations and validated IDs |
| Firecracker | KVM virtualization and guest devices | Dedicated VMM user, restricted filesystem view and cgroups |
| `tokendrain-guestd` | Codex supervision/proxy, runtime credentials, rotation and shutdown | Root inside one guest |
| Codex and its commands | Autonomous project work | Full guest access, assigned runtime credentials and permitted destinations |

Infrastructure operations sit behind explicit storage, VM, command-runner, and session interfaces. Test backends replace these dependencies without creating a separate execution architecture.

## Persistent state

The default state directory is `/var/lib/tokendrain`:

```text
tokendrain.sqlite3           Projects, runs, reports, events, settings
credentials/                Versioned encrypted credential records
daemon.lock                 Exclusive application-state ownership
locks/<project-id>.lock     Cross-process project storage leases
projects/<project-id>/
    vm.img                  Complete persistent development machine
```

The default master key is separate at `/var/lib/tokendrain-keys/master.key` and reaches the daemon through a systemd runtime credential. VM records/sockets live under `/run/tokendrain-vms`; host authentication subprocesses use `/run/tokendrain-auth`. Guest runtime files are in the guest's `/run/tokendrain` tmpfs.

SQLite contains relational projects, runs, executions, reports, usage observations, schedules, usage automations and their occurrences, events, secret metadata, GitHub app/installations/project bindings, and settings. A project execution holds its model/reasoning configuration; project and execution records retain Codex thread IDs. OpenAI account records are encrypted files, not plaintext database rows. JSON columns hold typed flexible structures such as run templates, provider observations, reports, and storage metadata.

Alembic migrations run before work is admitted. SQLite foreign keys, WAL, transactions, and uniqueness constraints provide durable coordination. Database transactions do not remain open across VM boot, network requests, or filesystem copies.

## Runs and executions

One run selects projects and a shared stopping policy. Each project gets its own execution, model, reasoning setting, VM, thread, status, report, and observations. A partial unique database index prevents a project from belonging to multiple queued or active executions.

Executions follow validated transitions:

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> preparing
    queued --> cancelled
    queued --> failed
    preparing --> starting_vm
    preparing --> stopping
    starting_vm --> running
    starting_vm --> stopping
    running --> stopping
    stopping --> completed
    stopping --> blocked
    stopping --> failed
    stopping --> cancelled
```

Run status aggregates its executions. Budget exhaustion stops an execution even if the project still has remaining work. Goal completion is a separate fact in the agent report.

The supervisor owns execution tasks through structured concurrency. It enforces global concurrency; the helper independently enforces platform maxima and aggregate memory reservations. A sequential run admits one of its own executions at a time, while unrelated runs may use other slots.

## Work lifecycle

1. Reserve the project in SQLite and acquire its asyncio/process storage lease.
2. Boot the project’s existing root filesystem with bounded resources and the current control bundle.
3. Import missing control closure paths into the persistent Nix store and start current guestd.
4. Connect over vsock, inject runtime credentials, and start Codex app-server.
5. Resume the saved thread when available or start one. Supply the goal, Kanban board, feedback, previous state, integration capabilities, and secret names/descriptions.
6. Read actual provider limits and request a substantial autonomous work unit. Save structured progress, then continue while the policy permits another turn.
7. Stop Codex, clear managed runtime credentials, stop the guest, and confirm VMM exit before releasing the project.

The agent inspects current files and processes before acting. Durable project state supplies context instead of continually replaying historical conversations. Feedback is cleared only after a turn returns a result, and only if its text still matches the instructions supplied to that session. A boot, authentication, or budget failure before any result leaves the feedback available for another run. Concurrently edited feedback is retained. Kanban mutations enforce human approval of Backlog in the host.

Reports include summary, completed/remaining work, blockers, change counts and commits when available, next action, structured task updates, and usage observations. Only final-answer items are parsed. Malformed final output fails visibly instead of starting another work turn. Earlier progress survives a subsequent provider or infrastructure failure.

## Budget boundaries

Usage rules select a limit identifier and/or provider-reported window duration, then compare used percentage. Multiple rules use ANY semantics. Tokendrain assigns no fixed meaning to `primary` or `secondary` windows.

Elapsed runtime is shared by the run, beginning when its first execution starts preparation. Preparation and earlier sequential executions consume that same runtime budget. It is monitored during turns. Usage and runtime boundaries follow the persisted graceful/hard policy; graceful allows a single 90-second wrap-up, hard allows no finalization turn. A separate turn watchdog handles overlong turns; cancellation and expiring credentials have their own interruption paths.

Account usage can change through other projects or external Codex sessions. Read responses and update notifications feed persisted observations. A percentage rule without an observable matching window fails visibly. Provider refusal and project completion are always natural boundaries. See [OpenAI authentication](openai-auth.md) for differences between account modes.

## Authentication and rotation

The host owns long-lived OpenAI and GitHub credentials. Guests receive current OpenAI access tokens, scoped GitHub installation tokens, and explicitly assigned generic secrets. Refresh tokens, imported master auth files, the application encryption key, and GitHub App private keys never enter a VM.

Per-account locks serialize OpenAI refresh. Rotation normally occurs at a turn boundary. When expiry requires interruption, the host waits for the terminal event, restarts and initializes Codex, and resumes the thread. The next request inspects existing work instead of replaying the interrupted action.

GitHub project bindings store one of three access modes. Serialized SQLite binding mutations reject incompatible writable modes for the same repository ID. The host reconciles a shared, tracked PR-only ruleset and verifies protection before starting writable guests. Separate administration tokens are used only for host policy operations. The GitHub provider centralizes and serializes narrowed guest token issuance for scoped project bindings. Guestd updates runtime Git credentials and process environment on rotation. Secret values enter the runtime environment; prompts contain their names and intended uses.

Replacing or disconnecting the OpenAI account, changing the GitHub App, and refreshing GitHub installation discovery require every execution to be terminal, including queued executions. A durable credential-change reservation prevents new runs from starting while those operations are in progress. This gate does not block automatic runtime token refresh. Changing one project's repository binding requires that project to be idle.

Adding, editing or deleting generic secrets also requires the affected project to be idle. Secret metadata changes and Run admission serialize in SQLite, so a queued execution cannot lose a credential while preparing its runtime environment. Values staged before a rejected change are removed unless the database already references them; an unavailable database leaves an encrypted orphan instead of risking deletion of a live credential.

## Execution defaults

Defaults saved through Settings survive daemon restarts and take precedence over the NixOS initial values. NixOS concurrency, CPU, and memory maxima remain enforced. Changing the default disk size affects newly created projects; existing disks require offline growth.

## Scheduling

A schedule stores a five-field cron expression, IANA timezone, enabled state, next occurrence, and ordinary run template. Creating the run and advancing the trigger happen transactionally. A unique schedule/occurrence key prevents duplicates.

Downtime is coalesced into one due occurrence rather than replaying every missed tick. If a selected project is already queued or running, the occurrence is skipped, an event records why, and the schedule advances. Local timezone matching skips nonexistent DST times; a repeated local time can trigger at two distinct UTC instants.

## Storage and restart recovery

Each project owns one persistent root filesystem, cheaply cloned from a Nix-built base at creation. The OS, homes, configuration, Nix store/database, tools and `/workspace` persist together. The current read-only control payload imports only missing Nix closure paths at boot; it never replaces project userspace or overlays the project store. Storage supports idle growth only. Directory fsync ordering and cancellation-safe I/O retain the lease until mutations finish.

On startup, the supervisor reconciles helper records and surviving VM services. It stops those machines before releasing reservations or admitting work. Interrupted active executions become failed with their disks preserved; queued executions remain available. Unconfirmed teardown keeps recovery blocked and the project reserved. Side-effecting turns are never automatically replayed after connection or process failure.

The helper writes root-owned records before resource creation, validates disk paths using pinned descriptors, and performs idempotent cleanup. [MicroVM operations](microvms.md) documents transient units, network policy, persistence, and KVM tests.

## API and browser

FastAPI exposes `/api/v1` and serves the packaged Vite build from the same origin. Administration-token login establishes an HttpOnly cookie. Mutations carry an explicit request header and undergo origin checks.

Events are persisted before notification. SSE uses database IDs for replay and comment heartbeats. The browser coalesces invalidation events, loads bounded historical logs, and appends live events. It does not poll every second. See [web/API.md](../web/API.md) for the full contract.

There is one administrator trust domain per installation. VM separation is not a multi-user authorization system for the control plane.


## Usage automations

The host evaluates usage rules at startup and every 15 minutes, refreshing account metadata when needed without model inference. Each matching provider limit/window/reset has a durable occurrence containing the saved run configuration. Automatic launches and website approvals use the ordinary transactional run-admission service. Pending approval requests are committed before ntfy delivery; notifications contain review links without authorization tokens.

Reset deadlines are fixed on the resulting run and enforced by the execution monitor independently of trigger cadence. They interrupt substantive work and graceful wrap-up and prevent queued executions from starting after reset. Editing or disabling rules cancels pending occurrences without changing existing runs.
