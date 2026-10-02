# Host ↔ guest protocol, version 1

The protocol is independent of Python. A Rust guest can implement the same
messages without changing the host. The production transport is Firecracker
vsock, guest port **4050**. The host connects to the VM's private Firecracker
vsock Unix socket, sends ASCII `CONNECT 4050\n`, and requires a response beginning
`OK `. Frames follow immediately. Guestd binds `AF_VSOCK`; it exposes no guest
TCP management listener. A Unix socket option exists for development tests.

## Framing and correlation

Each message is a four-byte unsigned big-endian payload length, followed by that
many UTF-8 JSON bytes. Payload lengths must be between 1 byte and 8 MiB inclusive.
Invalid JSON, an unsupported protocol version or a bad length closes the
connection. A peer reads exact lengths, so segmentation and coalesced frames work.

Request:

```json
{"version":1,"id":23,"method":"ping","params":{}}
```

Response:

```json
{"version":1,"id":23,"result":{"version":1,"ready":true}}
```

Error:

```json
{"version":1,"id":23,"error":{"code":-32002,"message":"rotate at a completed turn boundary"}}
```

Notification:

```json
{"version":1,"method":"turn/completed","params":{"threadId":"thread-id","turn":{"id":"turn-id","status":"completed"}}}
```

IDs are request-correlated and scoped to each connection. Notifications have no
ID. Both peers can initiate requests; no request is blindly retried on connection
loss. Each request has a deadline. Errors fail the pending operation rather than
pretending it succeeded. Disconnect fails every pending request. Notification
queues are bounded with backpressure and do not silently drop terminal events.

## Operations

| Method | Parameters | Behavior |
| --- | --- | --- |
| `ping` | `{}` | Protocol readiness |
| `guest_info`, `codex_status` | `{}` | Runtime status, paths and active thread/turn IDs |
| `credentials_set` | `openai`, `secrets`, optional `github_token` | Set initial runtime credentials; rejected while Codex runs |
| `credentials_clear` | `{}` | Stop Codex, remove managed runtime secret files and clear references |
| `codex_start` | `{}` | Start and initialize app-server; existing running instance is reused |
| `codex_stop` | `{}` | Stop app-server and its process group; idempotent |
| `codex_rpc_request` | `method`, `params` | Proxy a request and return the app-server result |
| `codex_rpc_notification` | `method`, `params` | Proxy a notification |
| `openai_token_rotate` | Runtime credential object, or `{openai: object}` | Restart, initialize and resume saved thread at a turn boundary |
| `github_token_rotate` | `{token: string}` | Replace tmpfs token; restart and resume so `GH_TOKEN` updates |
| `shutdown` | `{}` | Stop Codex, clear managed credentials and exit guestd; host tears down VM |
| `openai_refresh_required` | Codex reason and previous account ID | Guest→host request for fresh externally managed credentials |

The OpenAI runtime credential object contains:

```json
{"mode":"siwc","access_token":"<runtime-only>","expires_at":1790971200,"account_id":null,"plan_type":null}
```

For imported Codex credentials, `mode` is `chatgpt` and `account_id` is the
selected ChatGPT account/workspace. Neither form contains a refresh token, ID
token, imported auth file, host master key, or GitHub App private key.

`secrets` maps valid environment variable names to values. Protected process
configuration variables such as `PATH`, `CODEX_HOME`, `ACCESS_TOKEN` and
`LD_PRELOAD` are rejected as generic secret names. Values enter the app-server
child environment and `/run/tokendrain/secrets.json`. Git credentials use a
runtime credential helper limited to HTTPS `github.com`; tokens never appear in
clone URLs. GitHub CLI receives `GH_TOKEN` and `GITHUB_TOKEN`.

## Codex and event lifecycle

Guestd starts `codex app-server --listen stdio://` and uses newline JSON on its
stdin/stdout. It initializes exactly once per process, with client name
`tokendrain` and experimental API capability for the imported-token path. Host
`initialize` calls return the saved initialization result. `thread/start`,
`thread/resume`, `turn/start` and `turn/completed` update guest runtime state.
Persistent Codex home `/persist/codex` retains thread rollouts across VM boots.
Work occurs in `/workspace` with `approvalPolicy=never`,
`thread/start.sandbox="danger-full-access"`, and
`turn/start.sandboxPolicy.type="dangerFullAccess"`.

App-server notifications retain their original method and params. Additional
notifications are `guest/log` (redacted stderr), `guest/codex_exited`
(exit code) and `guest/failure` (supervisor failure). Normal restart suppresses
expected process-exit events. Unexpected process, JSON-RPC or event forwarding
failure closes the guest channel so the host detects failure even when it is not
consuming events. The supervisor must treat unexpected process exit or transport loss
as an infrastructure failure, preserve the latest thread ID, and reconcile state
before deciding whether to resume. Guestd never replays a turn after a restart.

`openai_token_rotate` and `github_token_rotate` return error `-32002` during an
active turn. The host may wait for its boundary, or interrupt it explicitly and
wait for `turn/completed` before rotation. Imported-token emergency refresh uses
Codex's server-initiated `account/chatgptAuthTokens/refresh` request; guestd asks
the host with an eight-second deadline and returns only replacement access-token
fields. Unknown interactive methods fail immediately: there are no approval
prompts during an autonomous run.

## Trust and cleanup

Only one host session is accepted per guest. Host disconnect stops Codex and
clears guestd-managed runtime files. All secret paths live under the guest tmpfs
`/run/tokendrain`. Normal shutdown terminates the app-server process group,
removes runtime files, and closes the connection. Production NixOS enables `--poweroff-on-shutdown`: after the shutdown RPC reply
is delivered and processes are cleaned up, guestd requests
`systemctl --no-block reboot`. The guest kernel uses `reboot=k`: Firecracker
intercepts this reboot and exits after Linux flushes filesystems. On x86, ordinary
`poweroff` only halts Linux and leaves Firecracker alive because the VM has no
ACPI power management. See the [Firecracker shutdown FAQ](https://github.com/firecracker-microvm/firecracker/blob/main/FAQ.md#how-can-i-gracefully-reboot-the-guest-how-can-i-gracefully-poweroff-the-guest).
The flag retains its historical name. The option is off for local protocol tests.

The guest itself is untrusted and unrestricted. Log redaction replaces known
secret values but cannot detect arbitrary encodings. An agent can deliberately
copy a runtime credential into its workspace; runtime-only injection prevents
accidental automatic persistence, not malicious persistence by guest software.
Host code treats all guest JSON, event text and report content as untrusted.
