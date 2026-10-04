# Development and testing

The supported deployment is Linux NixOS with KVM. Most application development and integration tests need neither KVM nor provider credentials. Use the explicit mock backend for those tests; it never performs real Codex work or pretends that a project goal was achieved.

## Toolchain

From the repository root:

```sh
nix develop
uv sync --locked --group dev
```

The flake shell supplies Python 3.12, uv, Ruff, mypy, Node/npm, Git, Firecracker, ext4 tooling, networking tools, and Nix formatting. It also supplies the native-library search path needed by Python wheels such as greenlet on NixOS. Run Python checks inside this shell; invoking an unrelated system interpreter or a venv without the shell's library path can produce misleading missing-library errors.

`flake.lock` pins Nix inputs. `uv.lock` pins the development environment. Nix production packaging builds Python dependencies from the locked nixpkgs input. `web/package-lock.json` pins frontend dependencies, which Nix imports for the static web build.

## Run the whole application locally

Build the web assets:

```sh
cd web
npm ci
npm run build
cd ..
```

Start an isolated development instance from the repository root:

```sh
TOKENDRAIN_AUTH_MODE=none \
TOKENDRAIN_BACKEND=mock \
TOKENDRAIN_STATE_DIR="$PWD/.dev-state" \
TOKENDRAIN_WEB_DIR="$PWD/web/dist" \
uv run tokendraind
```

Open `http://127.0.0.1:8742`; this local example disables login. For token mode supply `TOKENDRAIN_ADMIN_TOKEN_FILE` yourself. Mock mode creates a development key in that private state directory. Its execution report explicitly describes simulation and stops as blocked; it does not spend provider usage. Never point mock mode at production state.

For frontend hot reload, run `npm run dev` in `web/`. Vite serves `127.0.0.1:5173` and proxies `/api` to the daemon on port 8742. Start the daemon with `TOKENDRAIN_PUBLIC_URL=http://127.0.0.1:5173` when using that frontend so browser Origin checks agree. Use `127.0.0.1` consistently instead of switching between it and `localhost`.

The real backend is `firecracker` and requires the helper, immutable guest artifacts, a protected master-key file, and connected provider credentials. The NixOS module is the supported way to provision those boundaries. A development shell alone does not grant network administration or install the host firewall.

System status runs application checks in the unprivileged daemon and requests VM checks through authenticated, read-only helper diagnostics. The daemon keeps `PrivateDevices` and has no Linux capabilities; its lack of KVM/TUN visibility or privileged tools is expected. Helper connection or prerequisite failures remain visible in status. `sudo tokendrain doctor` also inspects the host environment and reports service failures. Nix is needed for these development/build commands, but the installed daemon does not require a host `nix` executable at runtime.

## Python checks

```sh
uv run ruff check .
uv run ruff format --check src tests
uv run mypy
uv run pytest -m 'not kvm and not nix'
```

Tests cover stop policies, window normalization, state transitions, cron/DST calculations, encryption/redaction, token refresh locking, JSON-RPC correlation and failure handling, guest credential rotation, migrations, service/API behavior, scheduling, and recovery. Real ext4 storage tests exercise image creation, copying, restore and resize using ordinary unprivileged files. HTTP/provider and VM/session dependencies have explicit fake implementations.

Use targeted test files while iterating, then run the full ordinary suite before a milestone. External account tests should never borrow credentials from a developer's home directory implicitly.

## Frontend checks

```sh
cd web
npm run typecheck
npm run format:check
npm run build
```

The browser suite uses Playwright with explicit API fixtures. It checks login/token storage, provider-derived window labels, project/run/schedule submission, task edits, secret descriptions, GitHub scope selection, SSE text rendering, cancellation, and mobile overflow.

On NixOS, use a Nix Chromium executable:

```sh
nix shell nixpkgs#nodejs_22 nixpkgs#chromium
export PLAYWRIGHT_CHROMIUM_EXECUTABLE="$(command -v chromium)"
cd web
npm test
```

The suite starts Vite at port 5175. Production code contains no sample project data. Screenshots and failure traces go into ignored `web/test-results/`; do not publish traces from sessions containing real credentials. [web/README.md](../web/README.md) describes frontend details and the HTTP contract.

An additional browser smoke uses the real daemon API. Start the dedicated mock instance described above, then run from `web/`:

```sh
TOKENDRAIN_LIVE_URL=http://127.0.0.1:8742 \
TOKENDRAIN_LIVE_TOKEN_FILE="$PWD/../.dev-state/admin-token" \
npm test -- tests/live-daemon.spec.ts
```

This opt-in test verifies `backend=mock` before changing application data. It creates and removes its own project and schedule, exercises tasks/feedback, described secrets, snapshots/restore/resize, an ordinary run/report/live events, history, and settings. The token is read directly from the protected file without printing it; trace recording is disabled. Use dedicated development state, never a production daemon. The fixture suite has ten tests; with the live smoke enabled, all eleven have passed against the implemented daemon.

## Real KVM tests

First check host access with `tokendrain doctor` and confirm `/dev/kvm` can be opened. The direct guest test uses no root, TAP changes, real account, or paid inference. It boots the actual NixOS guest and uses local Codex `command/exec` requests to exercise guest control and persistence.

```sh
export TOKENDRAIN_TEST_GUEST_ARTIFACTS="$(nix build .#guest-artifacts --no-link --print-out-paths)"
nix develop -c .venv/bin/pytest tests/integration/test_kvm_guest.py -m kvm -v
```

The test checks workspace/home state across boots, Nix database/overlay behavior, and runtime credential removal. It requires working KVM access and skips when its opt-in artifact configuration is absent. It does not establish OpenAI entitlement or validate a real OAuth login.

The privileged helper and network policy are tested inside disposable NixOS machines:

```sh
nix build .#checks.x86_64-linux.module -L
nix build .#checks.x86_64-linux.firecracker -L
```

The module smoke boots the service and checks its HTTP health endpoint. The Firecracker test needs nested KVM and exercises helper/systemd confinement, concurrent guests, simulated public egress, blocked host/LAN/interguest traffic, firewall reload behavior, persistence, and cleanup. Its network endpoints are in the test network, not public services. See [microVM documentation](microvms.md) for diagnostic commands and platform details.

## Live account acceptance

Provider mocks, the installed Codex protocol smoke, and real guest boots pass without borrowing developer credentials. These checks still require accounts configured by the administrator through tokendrain:

1. Import auth.json from an eligible Codex account in Settings.
2. Run a small disposable project with an available model, inspect its output and report, then run it again to verify continuation using real inference.
3. Keep a run active through token expiry to validate actual provider refresh and thread recovery. Mock tests cover the protocol and locking; they cannot establish account entitlement or provider behavior.
4. Inspect the account's actual usage windows and exercise a matching budget threshold. Use duration or provider exhaustion if the connected provider does not expose percentage windows.
5. Install a self-owned GitHub App on a disposable repository, grant a limited project scope, and verify a branch push and pull request. Repeat through installation-token renewal.

Live credential import, paid/plan inference, multi-hour provider rotation and GitHub writes have not been performed as part of the automated test suite. Keep those acceptance runs separate from production projects.

## Nix build outputs

```sh
nix build .#tokendrain
nix build .#tokendrain-guestd
nix build .#web
nix build .#guest-artifacts
```

Outputs are exported for `x86_64-linux` and `aarch64-linux`; real KVM validation should be repeated on the architecture being deployed. `nix flake check` includes VM checks and therefore has greater resource/KVM requirements than ordinary Python tests. A first guest build downloads a substantial development closure.

For a guest change, rebuild `guest-artifacts` before the KVM test. For a Python change, rerun relevant Python tests and the package build. For frontend changes, rerun type/build checks and relevant browser tests. Keep Alembic revisions forward-compatible with existing state; production migration is never `create_all()`.

## Repository map

| Path | Purpose |
| --- | --- |
| `src/tokendrain/api` | HTTP boundaries and authentication |
| `src/tokendrain/application.py` | Dependency ownership and service lifecycle |
| `src/tokendrain/db` | SQLAlchemy models and packaged Alembic revisions |
| `src/tokendrain/orchestration` | Runs, real/mock sessions, safe work boundaries |
| `src/tokendrain/auth`, `codex`, `github`, `credentials` | Provider adapters, JSON-RPC, token broker, encryption |
| `src/tokendrain/storage`, `vm`, `networking` | Auditable infrastructure backends |
| `src/tokendrain_guestd` | Guest process/credential supervisor |
| `web` | React/TypeScript UI, HTTP contract, browser tests |
| `modules`, `nix` | NixOS deployment, guest image and VM tests |
| `tests` | Python unit and integration tests |

External interfaces are version-sensitive. Check official provider documentation and the pinned executable's generated schema before changing Codex JSON-RPC, Firecracker, microvm.nix, or GitHub behavior. The [OpenAI](openai-auth.md), [GitHub](github-app.md), [guest protocol](guest-protocol.md), and [microVM](microvms.md) documents record implemented semantics and source links.
