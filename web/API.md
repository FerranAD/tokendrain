# Frontend API contract

All paths are under `/api/v1`. JSON responses use bare arrays for lists and objects for detail. IDs are strings. Timestamps are ISO 8601, nullable before start/finish. An error has `detail` (string or FastAPI validation errors). Mutations return the created/updated object except deletes (204 is supported).

Authentication: `POST /session {token}` establishes a same-origin HttpOnly cookie; `DELETE /session` signs out. An unauthenticated request returns 401. All fetch and SSE requests use same-origin credentials. POST/PATCH/DELETE requests are JSON and send `X-Tokendrain-Request: 1` as a CSRF defense in addition to server Origin validation.

`GET /events` is SSE. Default-message data is JSON with a `type`, optional `project_id`, `run_id`, `execution_id`, `message`, `timestamp`. Any message invalidates displayed API data (coalesced); `execution.log` messages also append to the live run log. Server must replay events with Last-Event-ID if supported. UI reloads automatically on reconnect; connection status appears in Run activity.

## Projects

- `GET /projects` → `Project[]`
- `POST /projects {name,description,default_model?,default_reasoning_effort?,initial_tasks?:[{title,description,column}],github?:{installation_id,repository_id,repository_name,permissions}}` validates and saves optional GitHub access together with the project.
- `GET /projects/{id}` → `Project`
- `PATCH /projects/{id}` accepts `name,description,next_run_feedback,default_model,default_reasoning_effort`
- `DELETE /projects/{id}` rejects active projects
- `GET /projects/{id}/executions` → `Execution[]`
- `GET /projects/{id}/storage` → `{virtual_size_bytes,allocated_bytes}`
- `POST /projects/{id}/storage/resize {size_gib:number}` grows total VM storage while idle.

Project: `{id,name,description,next_run_feedback,status,default_model,default_reasoning_effort,created_at,updated_at,last_run_at?,latest_report?:Report,storage_metadata?}`. Empty model means provider default. Each project owns one persistent development machine; Workspace exposes only its `/workspace` directory.

## Runs, limits, models

- `GET /runs` → `Run[]`
- `POST /runs RunTemplate` → `Run`
- `GET /runs/{id}` → `Run` (includes executions)
- `POST /runs/{id}/cancel {}`
- `GET /runs/{id}/events` → `Event[]` (bounded initial persisted log; SSE supplies updates)
- `GET /usage` → `UsageWindow[]`
- `GET /auth/openai/models` → `Model[]`

RunTemplate: `{projects:[{project_id,model,reasoning_effort}],stop_conditions:[{kind:"usage",window_minutes:number,used_percent:number,limit_id?:string}|{kind:"elapsed",seconds:number}|{kind:"provider_limit"}|{kind:"project_completed"}],parallel:boolean,threshold_mode:"graceful"|"hard"}`. Stop conditions use ANY semantics. Runtime and usage limits are optional; provider limit and completion are natural stopping conditions.

Run: `{id,status,created_at,started_at?,finished_at?,parallel,stop_conditions,threshold_mode,executions:Execution[]}`.
Execution: `{id,project_id,project_name?,run_id,status,model,reasoning_effort,started_at?,finished_at?,error?,termination_reason?,termination_detail?,threshold_mode?,interrupted,report?:Report,checkpoint_from_execution_id?,thread_id?}`. Execution outcome is separate from the last valid agent report.
UsageWindow: `{limit_id,name?,used_percent,window_minutes?,resets_at?,observed_at?}`. Names/window durations come from provider; UI never assumes primary or secondary semantics.
Model: `{id,name?,reasoning_efforts?:string[],is_default?:boolean}`. If model discovery is unavailable, UI permits entering a model manually.
Report: `{status,summary,completed:string[],remaining:string[],blockers:string[],changes?:{files_changed:number,insertions:number,deletions:number,commits?:string[]},suggested_next_action?,task_updates?:[{id,title,description,column,position}],usage?:{start:UsageWindow[],end:UsageWindow[]}}`.
Event: `{id?,type,message?,timestamp?,execution_id?,run_id?,project_id?,data?}`.

## Schedules

- `GET /schedules` → `Schedule[]`
- `POST /schedules {name,cron,timezone,enabled,run_template:RunTemplate}`
- `PATCH /schedules/{id}` accepts same fields
- `DELETE /schedules/{id}`

Schedule: `{id,name,cron,timezone,enabled,run_template,next_run_at?,last_run_at?}`.

## OpenAI / system

- `GET /auth/openai` → `{connected,valid,credential_error?,method?:"import",account_label?,usage_error?,connection_ok:null|boolean,connection_checked_at:null|number,reauth_required:boolean}`
- `POST /auth/openai/import {auth_json:object}` validates token format, stores credentials, checks metadata, and returns account/connection status.
- `POST /auth/openai/check` retries model/usage discovery immediately without starting inference and returns account/connection status.
- `DELETE /auth/openai`
- `GET /system` → `{version,backend,concurrency,vm_defaults:{vcpus,memory_mib,disk_gib},checks:[{name,ok,message,scope}],uptime_seconds?,active_executions?}`
- `PATCH /system {concurrency,vm_defaults:{vcpus,memory_mib,disk_gib}}`

Check `scope` identifies `daemon`, `helper`, or `mock` (local CLI diagnostics also use `host`). VM prerequisites are probed through the authenticated helper; missing devices or tools in the web daemon's restricted namespace are not reported as host failures. An unavailable helper produces a failing `helper` check.

## GitHub

- `GET /integrations/github` → `{configured,app_id?,app_slug?,installation_url?,installations:Installation[]}`
- `PUT /integrations/github {app_id,private_key,app_slug?}`
- `POST /integrations/github/sync {}`
- `GET /integrations/github/setup` → public, side-effect-free redirect to `/settings?github=installed`; query installation IDs are not trusted. Settings offers an authenticated `POST /integrations/github/sync` to perform discovery.
- `GET /integrations/github/installations/{id}/repositories` → `Repository[]`
- `GET /projects/{id}/github` → `ProjectGitHub|null`
- `PUT /projects/{id}/github {installation_id,repository_id,repository_name,permissions:{contents:"read"|"write",pull_requests?:"read"|"write",issues?:"read"|"write",actions?:"read"|"write",workflows?:"write"}}`
- `DELETE /projects/{id}/github`

Installation: `{id,account,permissions?:Record<string,string>}`. Repository: `{id,full_name,private?,default_branch?}`. ProjectGitHub is the PUT body.

## Notifications

- `GET /notifications/ntfy` → `{enabled,server_url,topic,rules,has_token,delivery:{last_sent_at?,last_checked_at?,last_error?}}`
- `PUT /notifications/ntfy {enabled,server_url,topic,rules,access_token?,clear_token?}` saves the destination and reminder rules. Missing access token preserves it; `clear_token:true` deletes it. Responses never include token values.
- `POST /notifications/ntfy/test` sends a test to the saved destination, including when reminders are disabled. Returns `{sent:true}` or a safe delivery error.

A rule is `{id,enabled,window_minutes,limit_id?,hours_before_reset,min_remaining_percent}`. Defaults: generated ID, enabled, weekly (`10080` minutes), any limit ID, within `12` hours, at least `80`% remaining. Up to 20 rules are supported. Successful reminders are deduplicated per rule/destination/window/reset across restarts. See [notification behavior](../docs/notifications.md).

## Generic secrets

- `GET /projects/{id}/secrets` → `Secret[]` (never values)
- `PUT /projects/{id}/secrets/{name} {value?,description}` (missing value preserves existing value)
- `DELETE /projects/{id}/secrets/{name}`
- `POST /projects/{id}/secrets/import {dotenv,descriptions:Record<string,string>}` (UI asks for per-variable descriptions before submission; malformed dotenv is rejected by server)

Secret: `{name,description,updated_at?}`.

## Kanban and workspace

- `GET /projects/{id}/tasks` → tasks with stable `id`, `title`, `description`, `column`, `position`, `origin` (`user`/`agent`).
- `POST /projects/{id}/tasks` accepts a list of `{id?,title?,description?,column?,position?}` mutations and returns the board. No ID creates a user task; supplied IDs must belong to this project.
- `DELETE /projects/{id}/tasks/{task_id}`.
- `GET /projects/{id}/workspace/tree?path=...` lists one directory with entry names, workspace-relative paths, kinds, sizes, and modification times.
- `GET /projects/{id}/workspace/file?path=...&allow_large=false` returns a bounded file preview (2 MiB normally, 32 MiB with `allow_large=true`).
- `GET /projects/{id}/workspace/download?path=...` downloads a regular file.
- `GET /projects/{id}/workspace/archive?path=...` downloads a directory as ZIP; an empty path exports the whole workspace.

All workspace access rejects active projects. Absolute paths, traversal, and symlink escape are rejected; symlinks and special files are not exported.

Execution outcomes include `termination_reason`, `termination_detail`, `threshold_mode`, and `interrupted`. `report` contains the latest valid checkpoint, with `checkpoint_from_execution_id` identifying its source if retained from an older execution. Execution status `stopped` covers budget/provider/no-progress stops. Run thresholds do not fabricate completion reports.

In auth-none mode `GET /session` includes `auth_mode:"none"`; login and cookies are unnecessary. Mutation request-header and Origin checks remain mandatory.

## Usage automations

All endpoints use the existing administrative authentication and same-origin mutation requirements.

| Method               | Path                                            | Behavior                                                                                                                                          |
| -------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| GET / POST           | `/api/v1/automations`                           | List rules / create a rule (201).                                                                                                                 |
| GET / PATCH / DELETE | `/api/v1/automations/{id}`                      | Read, update, or delete a rule (204 on deletion).                                                                                                 |
| GET                  | `/api/v1/automation-occurrences`                | Pending occurrences and up to 200 history entries; optional `automation_id` filter.                                                               |
| GET                  | `/api/v1/automation-occurrences/{id}`           | Configuration snapshot, status, errors, run ID, and latest observed `current_usage`.                                                              |
| POST                 | `/api/v1/automation-occurrences/{id}/authorize` | Refresh usage, recheck conditions, and atomically launch a pending request. Repeating a successful authorization returns the launched occurrence. |
| POST                 | `/api/v1/automation-occurrences/{id}/dismiss`   | Dismiss a pending request for this reset window.                                                                                                  |

Automation input:

```json
{
  "name": "Drain weekly allowance",
  "enabled": true,
  "mode": "approval",
  "trigger": {
    "window_minutes": 10080,
    "limit_id": null,
    "hours_before_reset": 12,
    "min_remaining_percent": 1
  },
  "run_template": {
    "projects": [{ "project_id": "project-id", "model": "", "reasoning_effort": "medium" }],
    "parallel": true,
    "threshold_mode": "graceful",
    "stop_conditions": [
      { "kind": "usage", "window_minutes": 10080, "used_percent": 100 },
      { "kind": "provider_limit" },
      { "kind": "project_completed" }
    ]
  }
}
```

`mode` is `automatic` or `approval`. Enabled approval rules require a saved ntfy topic. Trigger durations range from 1 to 525600 minutes, reset horizons are greater than zero and at most 168 hours, and remaining percentages range from 0 to 100. PATCH accepts a partial definition; changing a rule cancels its unlaunched occurrences for the current window.

Rules include `id`, `created_at`, `last_checked_at`, and `last_error`. Occurrences include `id`, nullable `automation_id`, `automation_name`, `limit_id`, `window_minutes`, `resets_at`, `matched_window`, `run_template`, `mode`, `status`, `created_at`, `notified_at`, `delivery_error`, `last_error`, and nullable `run_id`. Status is `ready`, `pending`, `launched`, `dismissed`, `expired`, `cancelled`, or `configuration_error`. Reads reflect expiration immediately, independently of worker cadence. Invalid authorization or admission conflicts return 409; the request remains pending on a temporary conflict.

Evaluation occurs at daemon startup and every 900 seconds. Each rule/provider-window/reset combination creates at most one occurrence. Run creation commits with the occurrence's launched state. Notifications open the configured public URL plus `/automation-occurrences/{id}` and contain no authorization token.

Run stop conditions now also accept `{"kind":"deadline","at":"2026-10-05T20:00:00Z"}` with a required timezone. Automation launches add a fixed reset deadline and bind otherwise unrestricted triggering-window usage stops to the matched limit ID. Deadline enforcement is a hard stop, including during graceful finalization. Run responses include nullable `automation_occurrence_id` and `automation_name`.
