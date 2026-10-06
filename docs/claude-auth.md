# Claude Code subscription

Choose **Settings → Agent → Claude Code**, then **Connect Claude Code**. Open the sign-in link, sign in with your Claude subscription, and return to Tokendrain. If Claude displays a login code, paste it into **Claude login code** and click **Complete login**. Login expires after ten minutes; **Cancel login** stops it immediately.

Tokendrain runs the official `claude auth login` command in an isolated temporary directory. Successful credentials are encrypted on the host; temporary login files are removed after success, failure, expiry, or cancellation. The host renews OAuth credentials. Guests receive only the access token required for subscription inference, plus the project's assigned secrets and GitHub token. No credential upload or API key is needed.

The global agent choice applies to all projects, manual runs, schedules, reminders, and usage automations. Finish or cancel queued and running executions before switching agents or replacing credentials. Each project retains separate model defaults and session history for Codex and Claude, while its workspace and task board stay shared. A saved run configured for the other agent uses the selected agent's project defaults. Switching cancels pending automation approvals.

Claude models use the `sonnet`, `opus`, and `haiku` aliases. Available versions and usage limits depend on your subscription. Claude runs headlessly inside the existing project VM, streams activity, and returns structured task checkpoints. Its session can resume on later turns and runs.

## Usage and reminders

Claude's 5-hour and weekly windows work with existing usage thresholds, reminders, and automations. Idle checks run every fifteen minutes without inference. Model-specific weekly limits appear when Claude reports them. Missing usage stays unavailable; it is never treated as zero.

Idle usage comes from Claude's private OAuth usage endpoint, which can change independently of the CLI. Responses are cached and rate-limited requests back off for at least fifteen minutes, including manual connection checks. If usage is unavailable, rules wait for a fresh observation. Live rate-limit events also update usage during turns. **Check connection** retries metadata discovery; **Reconnect Claude Code** replaces an expired login.

The integration uses subscription OAuth only. It does not fall back to Anthropic API keys, Bedrock, or Vertex billing. Project secrets cannot override Claude authentication or provider-routing environment variables.

## NixOS

The flake supplies Claude Code on the host and in the guest control bundle. `services.tokendrain.claudeCodePackage` controls the host login executable; see [all module options](nixos-options.md). Claude Code is an unfree package, explicitly allowed by the flake. Existing project machines need the current control bundle to run Claude.
