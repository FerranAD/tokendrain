# OpenAI connection

Tokendrain supports the official Sign in with ChatGPT public-client flow and an
advanced import of an existing Codex `auth.json`. These are different protocols;
a token from one is never sent through the other's provider configuration.

## Continue with ChatGPT

Open Settings → OpenAI / Codex, then **Sign in with ChatGPT** and follow the
secure sign-in link. First authorization
registers `tokendrain` using OpenAI's `dynamic_agent_client` entrypoint. No partner
key, client secret, or pre-registered developer client is needed. The browser lets
you authorize use of your ChatGPT plan and returns an issued client identifier.
Tokendrain retains that identifier with the validated account identity and reuses
it on subsequent sign-ins. Each host has a stable independent host identifier. Tokendrain generates and
persists a canonical `urn:uuid:<UUIDv4>` for this purpose. Arbitrary labels are
not accepted by the OpenAI interface; other documented formats are RFC 9278 JWK
thumbprint URIs and `did:key` identifiers.

Pause schedules and finish or cancel queued and active runs before connecting,
importing, reconnecting, or disconnecting an account. The same idle requirement
is checked when the browser returns from authorization, so avoid starting a run
before completing sign-in. Automatic access-token refresh during work does not
require this administrative pause.

The current public-client flow requires an HTTP loopback callback with the exact
host `127.0.0.1` and path `/auth/callback`. For a remote installation, open an SSH
tunnel from your browser's machine:

```console
ssh -L 8742:127.0.0.1:8742 your-nixos-host
```

Visit `http://127.0.0.1:8742` through the tunnel before signing in. A public reverse
proxy URL is not a supported callback for this public-client flow. The callback
listener is the running tokendrain web service; it is already listening before
an authorization URL is generated. Keep the same callback scheme, host and path
when reauthorizing an account; a different local port is allowed.

Tokendrain uses a new state, nonce and S256 PKCE verifier per attempt. Attempts
expire after ten minutes, are consumed once, and survive a daemon restart in the
encrypted credential store. Token exchange uses the issued client ID and exact
callback URI. ID tokens are signature-verified against OpenAI's published JWKS;
issuer, audience, expiry, subject and nonce are checked. An ID token alone does
not authorize inference: the `chatgpt.tokens.use.direct` granted scope is required.
Account selection and fresh authorization cannot overwrite another identity.

Access, refresh and retained ID tokens are encrypted on the host. Only a current
access token is injected into the guest process environment. The guest uses the
documented Responses provider named `openai_chatgpt_plan`, with the API base URL
`https://api.openai.com/v1`, `env_key="ACCESS_TOKEN"`, `wire_api="responses"`,
`requires_openai_auth=false` and `supports_websockets=false`.

## Token expiry and long runs

The host refreshes near expiry and serializes refreshes for an account. It writes
the new access token, expiration and rotated refresh token together using an
atomic encrypted-file replacement. Guestd restarts Codex with the new access
token, initializes the new server, and resumes the recorded thread. A rotation
is accepted only after a turn has completed or been interrupted. It never
replays the interrupted turn: the supervisor starts a recovery turn that inspects
existing workspace/thread state. Credential expiration is not a one-hour run
limit.

Signing out attempts the OIDC-discovered revocation endpoint and always clears
local tokens. If revocation cannot be confirmed, disconnect the app in
[ChatGPT Settings](https://chatgpt.com/settings/usage). Registration metadata is
retained for subsequent sign-in.

## Importing Codex `auth.json`

Import is an administrator compatibility path for an existing ChatGPT Codex
login. API-key-only files are rejected: they do not represent ChatGPT usage. A
single adapter reads the current Codex token structure. The full imported file
is encrypted in the host credential store. Token claims parsed from this local
file are compatibility metadata, not a validated OAuth identity.

When refresh is necessary, tokendrain creates a private temporary Codex home
under the host's `/run/tokendrain-auth` tmpfs, writes the imported file with mode
0600, starts the installed Codex app-server, and requests
`account/read` with `refreshToken=true`. Codex interprets and updates its own
credential format. The resulting file is immediately re-encrypted, and the
private temporary directory and process are removed. Tokendrain does not copy
Codex's private OAuth client or refresh endpoint into its implementation.

Only `accessToken`, selected `chatgptAccountId` and optional `chatgptPlanType`
enter the VM using app-server's experimental `chatgptAuthTokens` mode. Refresh
tokens do not enter the VM. If app-server requests a replacement after an
unauthorized response, guestd asks the host and responds using the documented
server-request protocol. This request has a short provider deadline; routine
proactive refresh reduces the need for emergency renewal.

## Usage and model availability

The dashboard probes an account before any VM or inference turn. For SIWC it
uses the official `GET https://api.openai.com/v1/models` account catalog, preserves
server ordering, and displays entries with `visibility="list"`. For imported
Codex accounts it starts a private host tmpfs app-server, logs in with only the
runtime access token, and reads `model/list` and `account/rateLimits/read`. The
probe never starts a thread or turn and removes its private home on exit. Codex
model listing can return a bundled catalog; an actual successful inference turn
establishes access to the selected model.

`account/rateLimits/read` and `account/rateLimits/updated` expose observed limits
where the provider supports them. The official app-server documentation describes
these for ChatGPT account mode. The SIWC custom Responses provider may not expose
these limits. Tokendrain must represent unavailable usage explicitly and refuse
to claim that a percentage stopping rule is enforceable without observations.
Duration/provider-exhaustion modes remain useful when percentages are unavailable.
Review account-side permissions and usage in
[ChatGPT Settings → Usage](https://chatgpt.com/settings/usage).

## Source and version notes

Implemented against official documentation inspected 2026-10-02:

- [Public-client registration and PKCE](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts, refresh and revocation](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Account-specific model catalog](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [SIWC with Codex app-server](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)
- [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server)
- [OpenAI identity verification](https://developers.openai.com/siwc/website)
- [Codex auth file implementation](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/storage.rs)

Live sign-in, actual account entitlements and provider quota responses require a
user's account and are not exercised by mocked integration tests.

Host auth subprocesses use a sanitized environment and fresh HOME/CODEX_HOME;
they never inherit administrator credentials or project configuration. Their
runtime root defaults to `/run/tokendrain-auth` and can be configured through
`TOKENDRAIN_AUTH_RUNTIME_DIR` or the daemon setting. The NixOS service creates
this private tmpfs runtime directory.

The installed Codex 0.159.3 protocol was exercised without account credentials.
Its generated schema and live requests establish an important spelling detail:
`thread/start.sandbox` is `"danger-full-access"`, while
`turn/start.sandboxPolicy.type` is `"dangerFullAccess"`. Tests cover this difference
because documentation examples can lag the executable schema.
