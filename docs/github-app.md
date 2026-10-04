# Connect your own GitHub App

Tokendrain uses an App you register and control. You choose which account or organization installs it, which repositories that installation exposes, and the smaller set of capabilities assigned to each project. Ordinary personal access tokens are not required.

The App private key stays encrypted on the host. Only short-lived installation tokens enter project VMs. This first integration targets `github.com` and one selected repository per project.

## 1. Register the App

In your personal or organization developer settings, open **GitHub Apps** and create an App with a unique name. The [official registration guide](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app) describes the current form.

Use these tokendrain-specific settings:

| Field | Value |
| --- | --- |
| Homepage | Your browser-facing tokendrain URL, or your project's public homepage |
| Setup URL | `<tokendrain-origin>/api/v1/integrations/github/setup` |
| OAuth callback | Unused by this integration |
| Request user authorization during installation | Off |
| Device flow | Off |
| Webhooks | Disable the Active checkbox; no webhook endpoint is required |
| Installation availability | Your account only, or other accounts if you intend to install into an organization |

For a local/tunneled deployment, the setup address can be `http://127.0.0.1:8742/api/v1/integrations/github/setup`; open the tunnel on the computer whose browser performs installation. For a reverse proxy, use its configured HTTPS origin. The Setup URL is a browser return destination, not a server-to-server webhook.

Tokendrain authenticates as the App installation. It does not request a GitHub user access token or attribute its API activity to an OAuth-authorized human. GitHub distinguishes the installation setup return from the OAuth authorization callback. [Setup URL behavior](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url).

Pause schedules and finish or cancel all queued and active runs before configuring the App, rotating its key, or refreshing installations. These host-wide operations reserve provider configuration against new runs until they finish. Automatic short-lived token renewal during a run is unaffected.

## 2. Choose maximum repository permissions

Set only the App permissions you expect to grant projects. Typical choices are:

| Capability in tokendrain | App permission | Project choices | Enables |
| --- | --- | --- | --- |
| Repository contents | Contents | No access / read / read and write | HTTPS clone/fetch; write adds branch/commit/push operations |
| Pull requests | Pull requests | No access / read / read and write | Inspect or create/update pull requests |
| Issues | Issues | No access / read / read and write | Inspect or create/update issues |
| GitHub Actions | Actions | No access / read / read and write | Inspect runs and logs; write adds dispatch, rerun and cancellation operations |

GitHub supplies repository metadata access as part of the installation model. Other permission categories are outside this version's project selector. In particular, tokendrain does not request administration or workflow-file management capabilities. GitHub can reject an operation requiring permissions beyond this subset.

After changing App permissions, approve the change for the installation in GitHub, then refresh installations in tokendrain Settings. Actions write access manages workflow runs; editing `.github/workflows` files requires the separate Workflows permission, which tokendrain does not currently offer.

These App permissions are a ceiling, not an instruction to grant every project all of them. A read-only investigation can receive only `contents:read`; a coding project may need `contents:write` and `pull_requests:write`. Repository branch rules, organization policy, and installation approval still apply.

## 3. Save the App credentials in tokendrain

After registering the App:

1. Record its **App ID** and URL slug, such as `my-tokendrain` from `github.com/apps/my-tokendrain`. The App ID is distinct from an OAuth client ID.
2. Generate a private key in the App's settings and keep the downloaded PEM private.
3. Open **tokendrain Settings → Your GitHub App**, enter the App ID and slug, and upload or paste the PEM.
4. Save the configuration. Tokendrain validates the App through GitHub using the supplied key, then encrypts the private key in its host credential store.

Do not add the PEM contents to your Nix configuration, project workspace, `.env` bundle, or a Git repository. This App key can mint tokens for its installations; it belongs to the trusted host.

## 4. Install and discover repositories

Use **Install app** in tokendrain or the installation page for your App. Choose the personal account/organization and select the repositories it should expose. If an organization requires approval, complete that process before expecting repository discovery to succeed.

After GitHub returns to tokendrain, sign in to the administration UI if necessary and choose **Discover installed repositories**. You can also use **Refresh installations** in Settings at any time. Then open a project's **GitHub** tab, choose the installation and repository, select capabilities, and save.

The public setup endpoint only redirects to Settings. It never trusts `installation_id` or other query values and performs no credential or installation mutation. The subsequent authenticated refresh enumerates installations using the configured App's own authority. GitHub explicitly warns that setup query parameters are forgeable; this single-owner application does not use them to link identities or grant repository access. [GitHub setup security](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url).

If the browser return cannot reach your private host, installation still exists on GitHub. Reopen tokendrain through your normal tunnel and refresh installations manually. You do not need to make a private host Internet-accessible for this integration.

## 5. Let a project work

Describe the desired repository work in the project's goal or next-run feedback. Give contents write permission when pushes are intended and pull-request write permission when the agent should create/update PRs. Start a run normally.

The agent receives the repository identity and allowed capabilities in its context. Git uses a runtime credential helper restricted to HTTPS `github.com`; the guest also has `gh`, with `GH_TOKEN` and `GITHUB_TOKEN` provided for its execution. Tokens are kept out of clone URLs and normal persistent credential files. The agent remains responsible for inspecting an existing workspace before cloning or modifying it.

The host signs short-lived App JWTs and requests an installation token containing the selected `repository_ids` and explicit `permissions`. It uses the returned expiry, caches tokens only within their usable interval, and re-mints before expiry. Guest rotation updates credentials at a safe turn boundary; long executions do not require manually supplied replacement tokens. GitHub supports narrowing these fields and returns the token's expiry. [Installation token API](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

The user-granted project permissions authorize autonomous work. There is no additional push, PR, or command approval dialog. Review the scope before starting a run.

## Changes, revocation, and troubleshooting

- **No installation appears:** verify the configured App ID/key, complete installation approval, and refresh. An App registration and its installation are separate objects.
- **A repository is missing:** check the installation's selected repositories on GitHub, then refresh and reselect it in the project.
- **Token issuance returns 403/422:** the requested subset may exceed the installation's current permissions or repository access. Accept any pending App permission change on GitHub, or narrow the project settings.
- **Push or PR fails:** inspect execution events, repository branch rules, required checks, and the selected capability. A token does not bypass repository policy.
- **Permissions change while a run is active:** stop the run, update the project binding, and start another execution. A binding cannot be changed while that project has a queued or active execution. Refreshing installation permissions first requires all projects to be idle.
- **Key rotation:** generate a new App PEM, reconfigure tokendrain, verify discovery, and remove the retired key in GitHub. The new key remains host-only.
- **Compromised access:** revoke the installation or credentials at GitHub. Disconnecting the project stops future injection but cannot erase copies already made by arbitrary guest software.

See [security](security.md) for storage/redaction limits and [OpenAI authentication](openai-auth.md) for the independent model-account connection. The implementation follows GitHub's documented API and treats token strings as opaque; it does not assume a fixed installation-token length.

## Publishing contributions

For a project that should publish a branch and open a PR, grant **Contents: write** and **Pull requests: write** on its selected target repository. PR write alone cannot publish the source branch. The project form warns about this combination before saving. Repository selection is searchable. Provider branch rules and installation permissions still apply.
