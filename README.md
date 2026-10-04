<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/tokendrain-logo-horizontal-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/tokendrain-logo-horizontal.png">
    <img alt="tokendrain" src="docs/assets/tokendrain-logo-horizontal.png" width="520">
  </picture>
</h1>

<p align="center">
  <strong>Put your Codex allowance to work before it resets.</strong><br>
  Give your projects an autonomous worker and a workspace that lasts.
</p>

<p align="center">
  <a href="#install-on-nixos"><img alt="Host: NixOS" src="https://img.shields.io/badge/Host-NixOS-5277C3?style=flat-square"></a>
  <a href="docs/microvms.md"><img alt="VMs: Firecracker" src="https://img.shields.io/badge/VMs-Firecracker-E36209?style=flat-square"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-22A06B?style=flat-square"></a>
</p>

<p align="center">
  <a href="#install-on-nixos"><strong>Get started</strong></a> ·
  <a href="#what-it-does">Features</a> ·
  <a href="#documentation">Documentation</a> ·
  <a href="docs/security.md">Security</a> ·
  <a href="docs/development.md">Contribute</a>
</p>

Tokendrain is a **self-hosted service for autonomous Codex work**. Set a project goal, approve tasks on its Kanban board, and run them now or on a schedule. Each execution gets a fresh Firecracker microVM with the project's existing source tree, installed tools, and development environment. Follow the work in your browser, review the report, and pick up where the last run stopped.

## What it does

| Feature | What you get |
| --- | --- |
| **A workspace that lasts** | Separate persistent workspace and environment disks keep source, tools, home, and caches ready for the next run. |
| **A fresh VM for every run** | Firecracker microVMs give each execution its own machine. Snapshot and selectively restore project disks from the UI. |
| **Work you control** | Approve tasks through Kanban, choose each project's model and reasoning effort, and leave feedback for the next run. |
| **Runs on your terms** | Start several projects with bounded concurrency, use timezone-aware cron schedules, and set runtime or observed usage thresholds. |
| **A window into the work** | Follow live events and reports, browse workspace files and previews while idle, and download the results. Light, dark, and system themes are included. |
| **Scoped integrations** | Import Codex `auth.json` with host-managed refresh, grant repository permissions through a GitHub App, and supply described runtime secrets. |

### From goal to next run

1. **Define the work.** Create a project, add approved tasks to Todo, and optionally connect a repository and secrets. Backlog stays reserved for your review.
2. **Let it run.** Choose a model and stopping conditions, then start immediately or save a schedule. The agent resumes In progress tasks before Todo and stops when approved work is finished or blocked.
3. **Review and repeat.** Read the report, inspect files, and update tasks or feedback. The next execution starts in a fresh VM with the same persistent project disks.

The service runs on **NixOS with KVM**, using one Python daemon and SQLite. Projects are managed through the application without per-project NixOS configuration. After a daemon restart, it reconciles surviving VMs and interrupted executions.

Agents have **root access inside their VMs** and execute commands without approval prompts. The VM, host firewall, and scoped runtime credentials define the boundary. Read the [security model](docs/security.md) before supplying credentials. Usage thresholds depend on provider observations and are not exact quota enforcement; see [First project](#first-project) for graceful and hard stop behavior.

## Install on NixOS

Use Linux NixOS with flakes enabled and working `/dev/kvm`. If the host is virtualized, its outer hypervisor must expose nested virtualization. The module configures users, KVM/TUN prerequisites, nftables, systemd services, and an immutable NixOS guest. Firmware virtualization settings and the outer hypervisor remain host responsibilities.

Add the input and module to your system flake, keeping your existing host and hardware configuration:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    tokendrain.url = "github:FerranAD/tokendrain";
    # To install directly from a checkout instead:
    # tokendrain.url = "path:/absolute/path/to/tokendrain";
  };

  outputs = { nixpkgs, tokendrain, ... }: {
    nixosConfigurations.my-host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        tokendrain.nixosModules.tokendrain
        {
          services.tokendrain = {
            enable = true;
            web = {
              listenAddress = "127.0.0.1";
              port = 8742;
            };
            auth = {
              mode = "token";
              adminTokenFile = "/run/secrets/tokendrain-admin";
            };
            concurrency = 2;
            microvm = {
              hypervisor = "firecracker";
              defaults = {
                vcpus = 4;
                memoryMiB = 4096;
                diskGiB = 40;
              };
              networking.allowLan = false;
            };
          };
        }
      ];
    };
  };
}
```

Use a GitHub revision containing this implementation, or the local checkout input during development. Then rebuild:

```sh
sudo nixos-rebuild switch --flake /etc/nixos#my-host
sudo systemctl status tokendraind tokendrain-helper
sudo tokendrain doctor
```

The first guest build includes Nix, Codex, compilers, and development tools. Creating projects later does not require a rebuild.

`diskGiB` is the initial capacity of **each** project disk. Sparse images and snapshots still consume host storage as data is written. Each active VM reserves its RAM plus 512 MiB of VMM overhead; the helper also protects a host memory reserve. [MicroVM operations](docs/microvms.md) explains resource limits and requirements.

### Open the interface

On the host, visit `http://127.0.0.1:8742`. From another machine, forward the port:

```sh
ssh -N -L 8742:127.0.0.1:8742 your-nixos-host
```

Open that local URL in your browser. Retrieve the administration token on the host:

```sh
sudo cat /run/secrets/tokendrain-admin
```

Paste it into the login form. This token controls the whole application; keep it private. The browser receives an HttpOnly session cookie and does not save the token in browser storage. Supply a token of at least 32 characters through `auth.adminTokenFile`. Tokendrain never generates it. Runtime secret paths work with agenix and sops-nix. Token mode without a file fails configuration/startup.

For remote HTTPS access, configure a reverse proxy and `services.tokendrain.web.publicUrl` to the browser-facing URL. Keep the daemon's listener private. For VPN, reverse-proxy authentication, or a trusted private network, set `services.tokendrain.auth.mode = "none"`. This hides login/sign-out and removes session requirements; Host/Origin/request-header checks still apply.

### Encryption key

By default, the module creates `/var/lib/tokendrain-keys/master.key`, readable only by root, and supplies it through a systemd credential. Back up this key: encrypted credentials cannot be recovered without it.

To use a key supplied at runtime by agenix, sops-nix, or another secret manager:

```nix
services.tokendrain.masterKeyFile = "/run/secrets/tokendrain-master-key";
```

Supply a raw 32-byte key or its URL-safe base64 encoding. Use a runtime **string path** with restrictive permissions. Never put a secret file's contents or a secret-bearing Nix path in the Nix store. Changing the key does not automatically re-encrypt existing credentials.

## First project

Choose **Light**, **Dark**, or **System** under Appearance. Edited forms show an unsaved-changes notice and warn before navigation; save explicitly to apply them. Kanban drag/reorder changes save automatically.

1. Open **Settings → OpenAI / Codex → Import Codex auth.json**. Upload/paste your Codex login file. Refresh stays managed by Codex on the host, so imported credentials work beyond their initial token expiry.
2. Create a project with a title, description, initial tasks, and default model/reasoning effort. Choose Todo for approved work or Backlog for later review. You can attach a repository through your [GitHub App](docs/github-app.md) during creation.
3. Add any project secrets with explanations of their permitted use. Model defaults and repository access can be changed later.
4. Select **Prepare run**. Choose projects, models, reasoning effort, and stopping conditions. Start the run or save it as a schedule.
5. Follow the activity timeline and checkpoints. Move/edit Kanban cards or leave feedback for the next run.
6. Run again: `/workspace`, the persistent home, Nix profiles, installed tools, and caches are reused in a fresh VM.

Usage windows come from provider metadata. A percentage rule without matching observations fails visibly rather than silently running without a budget.

**Graceful stop** is the default: reaching a threshold stops normal work, interrupts an active turn, and allows one wrap-up/checkpoint turn for up to 90 seconds. It may use some extra allowance. **Hard limit** interrupts when the threshold is observed and starts no further model work. Observations can arrive late, so hard mode is not exact quota enforcement. Both modes stop the execution with a usage termination reason; neither means the project is complete. An uncooperative interrupt gets a short bounded wait before infrastructure shutdown.

The Kanban columns are Backlog, Todo, In progress, and Done. Agents resume In progress first, then Todo. They can edit tasks and add discoveries to Backlog, but the host forbids agents from moving Backlog into approved work. Only users promote Backlog. No approved tasks means the agent should finish. Old Markdown task logs are discarded rather than migrated.

Execution outcomes (completion, blockers, cancellation, limits, infrastructure errors) are separate from the last valid agent checkpoint. Commentary never enters report parsing. Repeated unchanged checkpoints stop automatically.

Next-run feedback remains pending until an execution returns a turn result. Failed boot or authentication does not consume it. New feedback entered while work is running is retained for a later run.

## Operate and recover

```sh
tokendrain status
tokendrain projects
tokendrain runs
tokendrain doctor
sudo journalctl -u tokendraind -u tokendrain-helper -f
sudo journalctl -u 'tokendrain-vm-*' --since today
```

The CLI needs access to the configured admin-token file for API commands. On an installed host, run it as the service user, for example `sudo -u tokendrain tokendrain status`. Status and the Settings page check application prerequisites inside the daemon and query the authenticated helper for VM prerequisites. The daemon deliberately cannot see KVM/TUN devices or run privileged networking tools. A missing or unreachable helper is reported as a failure.

Use `sudo tokendrain doctor` for host diagnostics, including the root-owned master-key source and daemon/helper failures. Host and daemon filesystem views differ, so paths may differ between these commands. The host Nix CLI is useful for installation and development but is not a runtime requirement; the guest includes Nix for project development.

Before reconnecting/importing/disconnecting OpenAI, replacing the GitHub App key, or refreshing GitHub installations, pause schedules and finish or cancel queued and active runs. The API reserves these account changes against new run creation. Normal automatic token refresh continues during executions.

Execution defaults saved in Settings survive daemon restarts and take precedence over NixOS initial defaults. NixOS concurrency, CPU, and memory maxima still apply. Increasing the default disk size affects newly created projects; use a project's Environment controls to grow an existing disk.

Each project's **Environment** tab provides snapshots, selective restore, environment reset, and offline disk growth. Automatic snapshots link to their Run. **Workspace** provides a read-only file browser while the project is idle, with text/source, sanitized Markdown, and image previews. Download individual files, folders as ZIP, or the complete workspace as ZIP. Previews load automatically up to 2 MiB; larger files require confirmation and previews stop at 32 MiB. The privileged helper reads the selected disk in a read-only private mount namespace; symlinks and special files are omitted. Snapshots are disk copies, not RAM snapshots.

After an unexpected restart, surviving VMs are stopped before project reservations are released. Interrupted active executions become failed with an explanation; queued work can resume. A new run inspects and continues preserved work without blindly replaying a side-effecting turn.

Back up the state directory and encryption key together while executions are stopped. [Security and backup guidance](docs/security.md) covers the database, disks, credential store, and logs.

## Development and verification

```sh
nix develop
uv sync --locked --group dev
uv run ruff check .
uv run mypy
uv run pytest -m 'not kvm and not nix'
cd web
npm ci
npm run build
```

Test backends exercise orchestration without OpenAI credentials or KVM. Separate tests boot real Firecracker guests, verify persistent workspace/environment state, and exercise the helper and network isolation inside disposable NixOS machines. Browser tests cover UI/API interactions and mobile layout. Actual inference/account entitlements, multi-hour provider token rotation, and GitHub repository writes have not been exercised with real accounts; the [development guide](docs/development.md#live-account-acceptance) lists those remaining acceptance checks.

See [development instructions](docs/development.md) for the mock daemon, browser checks, real KVM tests, and NixOS tests.

## Documentation

- [Architecture and execution lifecycle](docs/architecture.md)
- [Security model and backups](docs/security.md)
- [OpenAI authentication and usage](docs/openai-auth.md)
- [GitHub App setup](docs/github-app.md)
- [MicroVMs, persistence, and networking](docs/microvms.md)
- [Versioned host ↔ guest protocol](docs/guest-protocol.md)
- [Development and testing](docs/development.md)
- [Web/API contract](web/API.md)

MIT licensed. See [LICENSE](LICENSE).
