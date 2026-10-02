# tokendrain

**Persistent projects. Disposable agents. Useful work from your available Codex usage.**

Tokendrain is a self-hosted NixOS service that runs Codex autonomously inside Firecracker microVMs. Give a project a goal, choose a model and stopping conditions, and let it work. Its source tree, installed tools, task log, and reports remain available for the next run.

Agents have root access inside their VMs and execute commands without approval prompts. The VM, host firewall, and scoped runtime credentials define the boundary. Read the [security model](docs/security.md) before supplying credentials.

## What it does

- Keeps separate persistent **environment** and **workspace** disks for every project.
- Runs several projects with per-project model/reasoning settings and bounded concurrency.
- Stops at useful turn boundaries on observed usage thresholds, runtime budgets, provider limits, completion, or blockers.
- Creates ordinary runs from timezone-aware cron schedules.
- Connects through Sign in with ChatGPT or an advanced Codex `auth.json` import, with host-owned refresh and runtime token rotation.
- Supplies narrowly scoped GitHub App installation tokens and optional described `.env` secrets.
- Provides a React UI for projects, tasks, feedback, reports, live events, schedules, snapshots, and account settings.
- Reconciles surviving VMs and interrupted executions after daemon restarts.

The service is one Python daemon with SQLite. It does not require Redis, a distributed queue, Kubernetes, or per-project NixOS configuration.

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
sudo cat /var/lib/tokendrain/admin-token
```

Paste it into the login form. This token controls the whole application; keep it private. The browser receives an HttpOnly session cookie and does not save the token in browser storage. The daemon creates the token on first startup.

For remote HTTPS access, configure a reverse proxy and `services.tokendrain.web.publicUrl` to the browser-facing URL. Keep the daemon's listener private. OpenAI's public-client sign-in still needs the loopback callback described in [OpenAI authentication](docs/openai-auth.md).

### Encryption key

By default, the module creates `/var/lib/tokendrain-keys/master.key`, readable only by root, and supplies it through a systemd credential. Back up this key: encrypted credentials cannot be recovered without it.

To use a key supplied at runtime by agenix, sops-nix, or another secret manager:

```nix
services.tokendrain.masterKeyFile = "/run/secrets/tokendrain-master-key";
```

Supply a raw 32-byte key or its URL-safe base64 encoding. Use a runtime **string path** with restrictive permissions. Never put a secret file's contents or a secret-bearing Nix path in the Nix store. Changing the key does not automatically re-encrypt existing credentials.

## First project

1. Open **Settings → OpenAI / Codex** and select **Sign in with ChatGPT**. Complete the browser flow. Importing an existing Codex `auth.json` is an alternative compatibility path.
2. Create a project and describe the goal, constraints, and how success should be checked.
3. Optionally attach a repository through your [GitHub App](docs/github-app.md) and add project secrets with explanations of their permitted use.
4. Select **Prepare run**. Choose projects, models, reasoning effort, and stopping conditions. Start the run or save it as a schedule.
5. Watch execution events and read the structured report. Edit the shared task log or leave feedback for the next run.
6. Run again: `/workspace`, the persistent home, Nix profiles, installed tools, and caches are reused in a fresh VM.

Usage windows come from actual provider metadata. They are not assumed to mean a particular five-hour or weekly allowance. Sign in with ChatGPT's custom provider may not expose percentage windows; use runtime or provider exhaustion when observations are unavailable. A percentage rule without matching observations fails visibly rather than silently running without that budget. [Authentication and usage details](docs/openai-auth.md) explains the two account modes.

Runtime and percentage budgets are checked between turns and are soft work boundaries. Cancellation, credential expiry handling, and the separate turn watchdog can interrupt an active turn. A run marked `completed` means its execution window ended normally; the report separately says whether the project's goal is complete.

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

Each project's **Environment** tab provides snapshots, selective restore, environment reset, and offline disk growth. Snapshots are disk copies, not RAM snapshots.

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

Test backends exercise orchestration without OpenAI credentials or KVM. Separate tests boot real Firecracker guests, verify persistent workspace/environment state, and exercise the helper and network isolation inside disposable NixOS machines. Browser tests cover UI/API interactions and mobile layout. Live provider sign-in, actual inference/account entitlements, multi-hour provider token rotation, and GitHub repository writes have not been exercised with real accounts; the [development guide](docs/development.md#live-account-acceptance) lists those remaining acceptance checks.

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
