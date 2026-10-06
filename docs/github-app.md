# Connect GitHub

1. Open **Settings → GitHub → Connect GitHub**.
2. Confirm the suggested App name on GitHub, or choose your own name.
3. Choose the accounts and repositories where GitHub should install the App.
4. Back in tokendrain, choose a repository for a project and select **Read only**, **Pull requests**, or **Direct write**.

Tokendrain handles registration, encrypted credentials, repository discovery and permission narrowing. No private key uploads, personal access tokens, user OAuth authorization or webhooks are needed. An organization’s policy may require an organization owner to approve or install an App requesting repository administration access.

## Choose how the AI may write

| Access | What the autonomous VM can do |
| --- | --- |
| Read only | Inspect, clone and fetch the repository; cannot publish changes. |
| Pull requests | Push feature branches and open/update PRs. GitHub requires a PR to update the default branch. This is the default writable mode. |
| Direct write | Publish directly to the default branch when appropriate, or use PRs. Existing repository rules still apply. |

These settings are enforced through GitHub App token permissions and, for Pull requests mode, GitHub branch rules—not just instructions to the agent.

Enable **Allow workflow file changes** only when the project needs to publish GitHub Actions workflow edits. It is unavailable in Read only mode.

Pull requests mode is enforced by GitHub. Tokendrain manages one shared `tokendrain: PR-only` ruleset per repository, targeting its current default branch regardless of its name. It requires pull requests, blocks force pushes and protects the default branch from deletion. Tokendrain requires zero approving reviews; your own rules can impose stronger requirements.

Human repository administrators can bypass tokendrain’s rule and push directly. The tokendrain App has no bypass entry. Tokendrain does not edit or remove your own rulesets or branch protections.

Projects sharing a repository can all use Pull requests, or all use Direct write. Those two writable modes cannot coexist for the same repository. Read only projects are compatible with either. Repository selection shows which other projects use it and disables the incompatible writable mode.

Tokendrain keeps protection while any project needs Pull requests. After the last such project changes access, disconnects or is deleted, it removes its own rule. If GitHub is unavailable, protection may remain until reconciliation succeeds. Writable Runs verify policy again before starting and at credential rotation boundaries; failure never silently grants Direct write.

## Private servers and Tailscale

Set `TOKENDRAIN_PUBLIC_URL` (or the NixOS module’s public URL option) to the address your browser uses to reach tokendrain, for example `https://tokendrain.example-tailnet.ts.net`. This configured address supplies GitHub’s return URLs. Tokendrain never builds them from a request’s Host header.

Your browser travels from tokendrain to GitHub and back to that private URL. GitHub needs no inbound network access to your machine. Tokendrain needs outbound HTTPS to GitHub’s API; webhook delivery is disabled.

## Host and VM authority

The App requests a permission ceiling that supports all three modes. Tokendrain’s trusted host temporarily uses repository administration authority only to manage its PR-only protection. Autonomous VMs **never receive repository Administration permission**.

App private keys stay encrypted on the host. App JWTs and policy tokens stay on the host. Each VM receives a distinct, short-lived token narrowed to its one repository and access mode. Tokens are injected only at runtime, rotated at safe boundaries, and stored by guestd in tmpfs credential files. The Git credential helper keeps tokens out of clone URLs; GitHub CLI receives `GH_TOKEN` and `GITHUB_TOKEN`. See [security](security.md) for storage and redaction limits.

## Recovery

Settings shows the connection name and repository count. **Manage repositories on GitHub** changes the installation selection; returning automatically refreshes available repositories.

Under **Advanced**, **Refresh repositories** retries discovery and policy reconciliation, **Reconnect GitHub** repeats registration, and **Disconnect GitHub** removes tokendrain’s rules and repository bindings before deleting host credentials. Reconnection creates a new App registration, preserves repository/access choices and discovers their new installations after you install it. The old App registration can be removed on GitHub after the new connection works.

If GitHub rejects PR-only protection, tokendrain shows the actionable reason and blocks writable Runs in that mode. GitHub rulesets are available for public repositories on Free plans and for private repositories on Pro, Team or Enterprise Cloud. Read only remains available; Direct write requires an explicit choice. Organization restrictions may also prevent enforcement. [GitHub ruleset availability](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets).

If a tracked rule was renamed, or a rule with tokendrain’s name exists without a tracked ID, tokendrain stops policy reconciliation rather than taking ownership of an unknown rule. Inspect the repository’s rules on GitHub and resolve that ambiguity before retrying.

Existing integrations created with the older raw-permission UI migrate to Read only. Choose a writable mode explicitly to enable publishing again.
