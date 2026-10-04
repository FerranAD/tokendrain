# Codex authentication and usage

Tokendrain supports **Import Codex auth.json** only. Log into Codex on your computer, then upload or paste its `auth.json` in Settings → OpenAI / Codex. API-key files are not subscription-usage accounts and are rejected. Settings shows the connected account, allows replacement, and disconnects locally.

The imported file stays encrypted on the host. Before token expiry, Tokendrain asks an isolated host Codex process to refresh a private tmpfs copy, then saves the renewed file centrally. Long unattended Runs keep this refresh behavior. Guests receive only current access-token/account metadata, never the imported file or refresh token. Credential rotation resumes the existing thread without replaying its interrupted turn.

Model discovery and usage probes use Codex app-server without starting inference. Percentage rules match actual provider window IDs/durations. Unknown windows fail visibly. The dashboard shows last observed usage and provider reset metadata.

Run configuration persists `threshold_mode`: `graceful` (default) permits one bounded wrap-up turn; `hard` interrupts on observation and performs no additional model work. Provider observation delays can overshoot the configured percentage. Outcomes are persisted separately from valid agent checkpoints.

Finish or cancel active/queued Runs before replacing or disconnecting credentials. Automatic refresh remains available during Runs. There is no OAuth callback, dynamic client registration, or host-ID setup.

## Reconnect when an imported file stops working

Import checks the access-token format and probes account models/usage without starting an inference turn. Settings distinguishes stored credentials from a checked connection and offers **Check connection** to retry. A malformed file is rejected before replacing credentials; a temporary provider/network failure remains visible and can be retried.

If the session is no longer usable, run `codex logout` followed by `codex login` on the computer where you use Codex, then replace the imported file with the newly written `auth.json` (normally `~/.codex/auth.json`, or under your configured `CODEX_HOME`). Do not copy a redacted example or assume an older file represents the currently working login. Replacing credentials preserves projects and workspaces; finish active/queued Runs first.
