# tokendrain web application

A React and TypeScript application served by `tokendraind`. It includes project management, persistent Kanban tasks and feedback, run preparation and reports, structured activity and raw events, recurring schedules, account connections, GitHub repository permissions, secret bundles, and VM storage capacity and growth.

The application always uses `/api/v1`. There is no demonstration backend or embedded sample project data. [API.md](API.md) describes the HTTP contract.

## Development

From the repository root, enter the development shell, then:

```sh
cd web
npm ci
npm run dev
```

Vite listens on `127.0.0.1:5173` and forwards `/api` to `127.0.0.1:8742`. Run the daemon separately and set its public URL to `http://127.0.0.1:5173` when testing through Vite so its Origin validation matches the browser. Open the configured URL consistently; `localhost` and `127.0.0.1` are different browser origins.

For a standalone frontend toolchain on NixOS:

```sh
nix shell nixpkgs#nodejs_22
```

The production build is `npm run build`. This runs strict TypeScript checking and writes static assets into `dist/`. Set the daemon's `TOKENDRAIN_WEB_DIR` to the absolute `dist/` path when running from a checkout. The Nix package installs this directory automatically. The daemon must serve `index.html` for client routes such as `/projects/<id>` and `/runs/<id>`.

## Checks

```sh
npm run typecheck
npm run format:check
npm run build
```

Browser tests exercise authentication, project creation, actual provider window metadata, per-project run settings, schedule templates, task edits, `.env` descriptions, GitHub capability narrowing, cancellation, SSE log delivery, and mobile overflow. They intercept the API with explicit test fixtures; production code does not contain those fixtures. They check UI behavior independently of the backend integration suite.

On NixOS, use Chromium from Nix rather than a downloaded browser binary:

```sh
nix shell nixpkgs#nodejs_22 nixpkgs#chromium
export PLAYWRIGHT_CHROMIUM_EXECUTABLE="$(command -v chromium)"
npm test
```

Elsewhere with a compatible Linux userspace, `npx playwright install chromium` installs the default test browser. `npm test` launches an isolated Vite server at port 5175. Screenshots and failure traces are written under the ignored `test-results/` directory.

To test the real daemon API, start a dedicated mock instance and set `TOKENDRAIN_LIVE_URL=http://127.0.0.1:8742` and `TOKENDRAIN_LIVE_TOKEN_FILE` to its protected admin-token file. `npm test -- tests/live-daemon.spec.ts` then exercises project editing, described secrets, storage controls, run reports/events, schedules, and settings through the browser. It refuses a non-mock backend and disables trace recording. See [development instructions](../docs/development.md) for the full command. Without those variables, this test is skipped.

## Authentication and live updates

The administration token is submitted once to `/session`. The server establishes an HttpOnly session cookie. Tokens and credentials are never saved in localStorage or sessionStorage. Secret form values are cleared after successful submission. API requests include `X-Tokendrain-Request: 1`; the server remains responsible for checking the session and Origin.

One same-origin `EventSource` connection receives server events. The client coalesces invalidation notifications, reloads current data on reconnect, and retains at most 500 live messages in memory. A run initially loads its bounded persisted event history, then appends matching SSE events. The UI does not continuously poll.

OpenAI's loopback callback needs to reach the daemon. For a remote host with the default callback port, run `ssh -N -L 8742:127.0.0.1:8742 user@your-host` on the browser's machine and open `http://127.0.0.1:8742`. If the host is configured with a different callback port, forward that port instead. This login step is separate from guest control; project VMs use the documented vsock protocol.

All report and log content is rendered as text. No model-provided HTML is executed. Destructive storage operations use explicit confirmation; active autonomous runs never request per-command approval.
