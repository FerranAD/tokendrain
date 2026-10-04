# Security model

**Tokendrain deliberately runs arbitrary autonomous code with root privileges inside each project VM.** Codex uses no command approvals and unrestricted guest access. An agent can install software, change files, execute network requests, make commits, and use the integrations granted to its project.

The host, its long-lived credentials, other projects, and the LAN are outside that authority. Firecracker/KVM, the host firewall, the privileged helper, and credential scoping enforce that separation. Prompts and secret descriptions are guidance, not security controls.

## What must be trusted

The host kernel/KVM, Firecracker, systemd, Nix-built platform closure, tokendrain host code, administrator, and supplied dependencies form the trusted computing base. The agent, its workspace, downloaded dependencies, and arbitrary guest programs are untrusted relative to the host.

Control-plane access grants authority to configure projects, allocate resources, supply credentials, and start unrestricted guest work. This version is designed for one trusted owner, not mutually untrusted web users. Protect the administration token and host account accordingly.

## Isolation

The web daemon runs as `tokendrain` without Linux capabilities and keeps systemd's `PrivateDevices` isolation. A separate privileged helper exposes only VM start, stop, list, read-only diagnostics, and offline project workspace listing, preview, and download operations over a local Unix socket. It verifies peer credentials and accepts validated project/execution UUIDs plus bounded resource values. Requests cannot supply arbitrary host commands, filenames, mounts, or Firecracker configurations. Workspace operations accept only project IDs and validated workspace-relative paths, resolve path components without following symlinks, and reject active projects.

System status checks application prerequisites in the daemon's own environment and obtains VM prerequisites through the helper's authenticated diagnostics operation. That operation inspects fixed resources; callers cannot select paths or commands. An unavailable helper is a failed check. KVM/TUN visibility and privileged networking executables are not requirements of the web daemon, and diagnostics do not grant it additional device access or capabilities.

The helper opens project disks through directory descriptors with `O_NOFOLLOW`, rejects non-regular/hardlinked images, and pins the descriptors before creating systemd bind mounts. The VMM runs as `tokendrain-vm` inside a restricted root-directory view with dropped capabilities, no-new-privileges, device restrictions, and cgroup limits. It does not see the application's credential store, master key, database, other project disks, or host home directories. The guest receives block devices rather than writable host directory shares.

Each VM has a separate TAP and source address. Default nftables policy allows IPv4 public Internet egress and blocks host input, private/link-local/metadata ranges, connected LAN prefixes, inter-project traffic, source spoofing, and IPv6. Publicly numbered directly connected LANs are included in discovered blocked prefixes. Route changes refresh the policy; normal firewall reloads deny egress until the helper rebuilds the guards atomically. Stopping the managed firewall stops dependent VMM services first.

`services.tokendrain.microvm.networking.allowLan = true` deliberately permits private/LAN destinations. Host input and other guests remain blocked. Do not manually remove the managed firewall table while machines are running. Internet access also permits agents to communicate with public services, including public endpoints you operate.

CPU, guest memory, process count, concurrency, and disk capacities are bounded. Host free storage still needs monitoring: sparse disks can grow to their configured sizes and non-reflink snapshots need real capacity. Isolation is not a promise of unlimited resource availability.

## Credentials and retained data

| Material | Persistent location | Enters the guest? |
| --- | --- | --- |
| Application master key | Root-only runtime source; default `/var/lib/tokendrain-keys/master.key` | No |
| OpenAI refresh/retained identity credentials | Encrypted host credential store | No |
| Imported Codex master `auth.json` | Encrypted host credential store; temporarily interpreted under host `/run` | No |
| Current OpenAI access token | Host credential record and transient guest runtime | Yes, only current runtime credential |
| GitHub App private key | Encrypted host credential store | No |
| Scoped GitHub installation token | Short-lived host memory and guest runtime | Yes, selected repository/capabilities |
| Generic project secrets | Encrypted host credential store | Yes, assigned project's runtime environment |
| Project files, tasks, descriptions, reports and logs | Project disks and/or SQLite | Project context and files as needed |

Credential records use `cryptography` AES-256-GCM with a fresh 96-bit nonce, authenticated record names, and a versioned `TDCR` format. Writes replace protected files atomically. The master key is a raw 32-byte key or its URL-safe base64 encoding; it is not a password. Encryption does not protect against a compromised host administrator or a compromised daemon that can use its loaded key. Python does not guarantee secure zeroization of all copies in process memory.

The module's generated key is outside the application state directory and enters the daemon through systemd `LoadCredential`. Alternatively, provide `masterKeyFile` as a runtime string path managed by your secret manager. Never interpolate secret contents into Nix expressions or use a secret-bearing Nix path: store paths are not private. Keep the existing key when upgrading; replacing it is not a supported key-rotation procedure.

Project disks and ordinary database content are not encrypted by the credential store. A task description, report, source file, or log can itself contain sensitive information. Use host storage encryption if your threat model includes offline access to those files.

## Runtime secrets are permissions

Generic secrets are available to any software in the project's VM. Their descriptions help the agent understand intended use, but cannot restrict a key's actual provider permissions. Prefer credentials that are already scoped appropriately by the external service.

Guestd injects values through child-process environment variables and files under `/run/tokendrain`, which is tmpfs. Managed credentials are removed on shutdown/disconnect. Snapshots of the environment/workspace disks do not include those runtime paths. However, guest root can copy a value into a persistent file, print it, or send it to a public endpoint. Runtime injection prevents default persistence; it cannot prevent intentional retention or disclosure by unrestricted guest code. Host swap and crash dumps also need appropriate protection if they are enabled.

Known values are redacted from application execution logs where possible. Literal matching cannot remove every encoded, transformed, fragmented, or previously unknown secret. Firecracker console output is untrusted and may appear in the system journal without application redaction. Restrict journal readers, backup access, and retention.

Git credentials use a runtime helper restricted to HTTPS `github.com`; tokens are not placed in repository URLs. The GitHub App private key remains on the host. Provider-side repository permissions, branch rules, and installation scope still apply. See [GitHub setup](github-app.md).

## Web access

The default listener is `127.0.0.1:8742`. Use an SSH tunnel for administration or a trusted HTTPS reverse proxy with the configured `web.publicUrl`. Do not expose the daemon as an unauthenticated network service.

In token mode, the administrator supplies `auth.adminTokenFile` as a runtime string path containing at least 32 characters. Tokendrain never generates the admin token; a missing source fails configuration/startup. Runtime files managed by agenix or sops-nix are supported. Login creates a signed, expiring HttpOnly session cookie; the token is not retained in browser storage. API clients can also authenticate with the admin token.

Auth-none mode omits login and session authentication; use external access control such as a VPN or authenticated reverse proxy. Both modes retain Host/Origin checks and the required mutation request header. Workspace Markdown is sanitized before rendering; agent reports and logs are displayed as text.

Codex credentials are imported from `auth.json`; there is no provider login callback. GitHub's setup callback only redirects to Settings and trusts no installation query parameters. An authenticated, origin-checked refresh discovers installations using the configured App credentials. See [Codex authentication](openai-auth.md) and [GitHub setup](github-app.md).

## Recovery and backups

A daemon process lock prevents two controllers from owning one state directory. Database reservations, process-level project leases, and helper attachment checks prevent simultaneous writable attachment. After restart, surviving VMMs must stop before reservations are released. An unconfirmed teardown leaves the project reserved for reconciliation.

For a consistent backup:

1. Pause schedules and let active work stop, or cancel it through the UI.
2. Stop `tokendraind` and confirm no `tokendrain-vm-*` service remains active. Resolve any unconfirmed teardown before copying images.
3. Back up the complete state directory, the actual master-key source, and host/flake configuration. Include SQLite sidecar files if present; copying only the main database from a live service is not a consistent backup.
4. Protect the backup as a full copy of project data and credentials. Keep another tested copy of the master key.
5. Restart the daemon and re-enable schedules deliberately.

Restore the matching state and key while services are stopped, with original ownership and restrictive permissions. A workspace-only snapshot restore does not restore database history, account credentials, or schedules. Snapshots on the same host are convenient rollback points, not independent backups.

If a credential may have been disclosed, revoke or rotate it at its provider. Deleting a tokendrain secret prevents future injection but cannot remove copies already made by guest software. Disconnecting a project integration does not rewrite its workspace history.
