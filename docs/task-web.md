# Frontend task log

- [x] Define versioned HTTP/SSE contract in `web/API.md`.
- [x] Build dashboard and project management, including persistent task/feedback editing and task progress derived from checkboxes.
- [x] Build run preparation, execution detail, reports, cancellation, and SSE event delivery.
- [x] Build schedules, session/OpenAI authentication, GitHub App setup and narrowed repository permissions, described `.env` bundles, and environment/snapshot controls.
- [x] Build and strictly type-check frontend; document workflow in `web/README.md`.
- [x] Run ten Chromium fixture tests covering the API contract, login storage behavior, project/run/schedule creation, task edits, secret descriptions, GitHub permissions and setup return, cancellation, SSE text rendering, and mobile layout.
- [x] Inspect desktop dashboard and mobile environment screenshots.
- [x] Run the browser against the real daemon with explicit mock storage/execution: project creation, tasks/feedback, secrets, snapshots/restore/resize, run/report/SSE, history, schedules, and defaults.
- [x] Add authenticated GitHub discovery prompt after the public setup callback; ignore returned installation identifiers.

Validation: `npm run build` passes; `npm test` passes 11/11 with Nix Chromium and the live-daemon opt-in variables. Ten browser tests use explicit intercepted API fixtures; the eleventh uses the real daemon API and persistent SQLite with a mock VM backend. No browser JavaScript errors occurred. Real OpenAI sign-in and GitHub installation/repository writes require the user's provider accounts.

## Graceful restart checkpoint (root)

Historical checkpoint below; all listed unfinished frontend work was completed after resuming.

User requested stop while frontend work was in progress. Frontend agent interrupted
before final handoff to ensure no background implementation continues during restart.
Files currently present: package.json/package-lock.json, index.html, vite.config.ts,
tsconfig.json, public/favicon.svg, src/api.tsx, projects.tsx, runs.tsx, settings.tsx,
types.ts, ui.tsx. Check for any additional files on resume. Application entrypoint,
dashboard/router and styles may be missing. Do not assume frontend builds yet.

API contract in API.md agreed with root. Models route changed to
/api/v1/auth/openai/models; cookie session POST /api/v1/session {token}, DELETE session;
mutations send X-Tokendrain-Request:1; SSE /api/v1/events. Typecheck/build next using
Nix-provided node/npm, cache under /tmp. Components should be reviewed against final
host schemas before finishing. No frontend build verification at root checkpoint.
