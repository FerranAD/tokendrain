# Frontend task log

- [x] Define versioned HTTP/SSE contract in `web/API.md`.
- [x] Build dashboard and project management, including persistent task/feedback editing and task progress derived from checkboxes.
- [x] Build run preparation, execution detail, reports, cancellation, and SSE event delivery.
- [x] Build schedules, session/OpenAI authentication, GitHub App setup and narrowed repository permissions, described `.env` bundles, and environment/snapshot controls.
- [x] Build and strictly type-check frontend; document workflow in `web/README.md`.
- [x] Run nine Chromium browser tests covering the API contract, login storage behavior, project/run/schedule creation, task edits, secret descriptions, GitHub permissions, cancellation, SSE text rendering, and mobile layout.
- [x] Inspect desktop dashboard and mobile environment screenshots.

Validation: `npm run build` passes; `npm test` passes 9/9 with Nix Chromium. Browser tests use explicit intercepted API fixtures. A live-daemon browser smoke remains an integration check once host endpoints are available; provider authentication still requires the user's real account.

## Graceful restart checkpoint (root)

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
