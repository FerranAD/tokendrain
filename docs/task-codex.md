# Codex, authentication and guest implementation task log

- [x] Inspected current official SIWC registration, session, identity and app-server docs; inspected Codex auth file source.
- [x] Typed asynchronous bidirectional JSON-RPC with request ownership, deadlines, notification backpressure, connection failure and server requests.
- [x] Versioned framed host/guest protocol over Firecracker vsock handshake, with development Unix transport.
- [x] Guest process supervisor; runtime-only credentials; no approval/full-access Codex configuration; GitHub helper and token rotation; restart/resume without turn replay.
- [x] Public-client SIWC dynamic registration, S256 PKCE, one-shot encrypted state, OIDC JWT verification, per-account central refresh lock, revocation and returning account validation.
- [x] AES-GCM versioned credential files, atomic durable replacement, permissions, authenticated name binding and redaction.
- [x] Isolated import compatibility adapter; host-side Codex refresh in tmpfs; guest receives only access token.
- [x] Unit/integration tests: encryption/tamper, PKCE/OIDC, refresh contention, import metadata, framing, bidirectional JSON-RPC/errors/timeouts, fake Codex subprocess rotation and persistent thread resume.
- [x] Dedicated authentication and protocol documentation with source links and live-system limitations.
- [ ] Live account sign-in, refresh and provider limit observation require user credentials.
- [ ] Full guest boot and actual Codex inference to be verified with the integrated Nix/Firecracker deployment.

Local milestone: 14 focused tests passed; Ruff and strict mypy run on owned application modules.

## Graceful stop checkpoint

Stopped at user request to restart Codex after host KVM/TUN permissions change.
No services or background test processes were intentionally left running.

Implemented files: `src/tokendrain/{auth,credentials,codex}/`,
`src/tokendrain/protocol.py`, `src/tokendrain_guestd/`,
`tests/unit/test_credentials_auth.py`, `tests/unit/test_codex_protocol.py`,
`tests/integration/test_guest_codex.py`, and this task log plus the two protocol/auth docs.
The root agent owns project-wide commits; no child commit was made.

Final checks at this checkpoint:

```console
.venv/bin/ruff check src/tokendrain/codex src/tokendrain/auth src/tokendrain/credentials src/tokendrain/protocol.py src/tokendrain_guestd tests/unit/test_credentials_auth.py tests/unit/test_codex_protocol.py tests/integration/test_guest_codex.py
.venv/bin/mypy src/tokendrain/codex src/tokendrain/auth src/tokendrain/credentials src/tokendrain/protocol.py src/tokendrain_guestd
.venv/bin/pytest tests/unit/test_credentials_auth.py tests/unit/test_codex_protocol.py tests/integration/test_guest_codex.py -q
```

All pass: 12 application modules typechecked, 14 tests passed.

Integration contracts:

- `GuestClient.connect(vsock_path, port=4050, request_handler=callback)` returns a framed connection. `.request(method, params)` proxies Codex slash methods and `initialize`; underscore lifecycle methods are direct guest operations. `.notifications` is an asyncio queue of `RpcNotification`.
- `CodexClient(guest)` supports initialize, start_thread(model, cwd, thread_id), start_turn(thread_id, prompt, model, effort, output_schema), read_thread, read_rate_limits, models, interrupt.
- `credentials_set`: `{openai: RuntimeCredentials.model_dump(), secrets: {name: value}, github_token: optional string}`. Start using `codex_start` (initializes internally); subsequent initialize is idempotent.
- `openai_token_rotate`: direct runtime credential dict or `{openai: dict}`. `github_token_rotate`: `{token: string}`. Both require a completed/interrupted turn and restart/resume automatically.
- Host request handler receives `openai_refresh_required` and must return a runtime credential dict within eight seconds. This is imported-token emergency renewal; proactive renewal is preferable.
- `OpenAIAuthManager(store, httpx_client, stable_host_id)` methods: begin_sign_in, complete_sign_in, import_auth_json, accounts, runtime_credentials, sign_out. Public account models strip credential fields.
- Guest entry point is `tokendrain_guestd.main:main`, port4050, `/persist/codex`, `/workspace`, `/run/tokendrain`. Host import refresh needs Codex installed and writable host tmpfs `/run/tokendrain/auth`.

Exact next actions after restart:

1. Run project-wide Ruff/mypy/pytest and resolve integration mismatches against root API/orchestration.
2. Exercise actual packaged Codex with app-server initialize/model list/account-rate-limit schema without exposing current workspace credentials.
3. Verify Nix guest boot and the framed vsock control path after KVM permissions are available.
4. Exercise SIWC or imported credentials only when an account is explicitly configured through tokendrain; do not reuse the implementation agent's credentials.
5. Ensure supervisor treats `guest/codex_exited` and connection loss as failures, and handles unavailable SIWC custom-provider rate-limit observations explicitly.
6. Review ownership of guest notification/stderr/watch tasks: tasks are retained, cancelled and gathered at stop; improve immediate propagation of an unexpected forwarding-task failure so a live run cannot wait indefinitely.
7. Real quota/entitlement and emergency refresh deadlines remain external integration risks; preserve safe turn boundaries and never replay side-effecting turns blindly.

## Resumed milestone

- Added `auth.probe.AuthProbe.read()` for model/usage metadata before Runs. SIWC
  uses the official account model endpoint; import probes use access-token-only
  Codex RPC in fresh tmpfs with sanitized environment and no inference.
- Centralized host subprocess ownership/cleanup in `auth.process.isolated_codex`.
  Runtime directory is `/run/tokendrain-auth`; manager/probe permit an explicit path.
- Verified real installed Codex 0.159.3 with no inherited credentials: initialize,
  account/read (account null), model/list, explicit command/exec, thread/start.
  Found and fixed thread sandbox enum drift: kebab-case thread setting versus
  camel-case turn policy. Generated executable schema confirmed it.
- Guest TaskGroup now propagates process/RPC/event forwarding failures and closes
  the host channel. Expected restart exits are suppressed. RPC errors preserve
  provider error data and concurrent server requests are bounded.
- Guest `--poweroff-on-shutdown` drains the shutdown response, cleans up and asks
  systemd to power off. Infrastructure agent integrated flag and successfully
  booted the previous guest artifact plus actual Codex command execution in KVM.
- Added OAuth failure/signout, probe, actual installed protocol, process crash and
  forwarding-failure tests. Latest focused result: 22 tests passed, Ruff clean,
  strict mypy clean across 14 application modules.

Remaining external validation: user-configured live OpenAI sign-in, account
entitlements, real token refresh and provider usage responses. No implementation
agent credentials were read or reused. Integrated full KVM persistence test is
owned by the infrastructure agent.
