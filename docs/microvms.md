# MicroVMs and project storage

Every project has a persistent development VM. Tokendrain attaches its current read-only control bundle whenever that VM runs. Creating a project, running it, or changing its Kanban board never requires `nixos-rebuild`.

```text
Project VM       → persistent
Tokendrain code  → current every Run
Credentials      → temporary
```

## Persistent machine, current control

Nix builds a base NixOS filesystem once. Project creation cheaply clones it using a reflink or sparse copy, then grows it to the configured total capacity. Each project owns one writable root filesystem. `/workspace` is an ordinary directory within it; `/root`, `/etc`, `/opt`, `/var`, Nix profiles, the Nix database, and project-installed store paths persist naturally between Runs. There is no selected list of retained directories.

The base includes Git, curl, jq, Nix, Python, Node, Rustup, a C/C++ compiler, CMake, Make and GitHub CLI. The agent has root authority inside its VM and can install tools or change its OS. A project can damage its own machine; an unbootable machine fails its Run clearly. Tokendrain still stops the VMM independently. There is no reset or snapshot workflow.

The host supplies the supported kernel/initrd, the project's writable root disk, and a small read-only control volume. The persistent OS profile supplies userspace; a host upgrade never clones a new machine over an existing project. The stable boot contract mounts the control volume at `/run/tokendrain-control` and executes its `activate` entrypoint.

The control volume contains a local Nix binary cache for current guestd and Codex, a manifest, and the activation script. Activation uses `nix copy` to import missing closure paths and register their references in the project's own persistent store/database. The control volume is never mounted over `/nix/store`. Current control roots protect the active closure from garbage collection; updates do not delete old paths. Project profiles and references keep their dependencies alive normally.

Activation starts the current guestd and passes an absolute path to the current Codex binary. A project-installed `codex` cannot replace that managed process. Managed credentials remain under `/run/tokendrain` on tmpfs; Codex conversations and non-secret state live in the project's home directory.

Implementation artifacts are `base.img`, `control.img`, and `projects/<project-id>/vm.img`. These are private infrastructure details, not product storage domains. This development storage format intentionally has no conversion from older installations.

## Host requirements and installation

Use Linux NixOS on x86-64 or aarch64, with hardware virtualization exposed to the host and enough RAM for the selected concurrency. Firecracker needs read/write access to `/dev/kvm`; TAP networking needs `/dev/net/tun`. Nested virtualization must be enabled if the NixOS host is itself virtualized. The module loads KVM/TUN and adds its dedicated VMM user to `kvm`; firmware or an outer hypervisor may still need configuration.

```nix
services.tokendrain = {
  enable = true;
  web.listenAddress = "127.0.0.1";
  web.port = 8742;
  auth.mode = "token";
  auth.adminTokenFile = "/run/secrets/tokendrain-admin";
  concurrency = 2;
  microvm = {
    hypervisor = "firecracker";
    defaults = {
      vcpus = 4;
      memoryMiB = 4096;
      diskGiB = 40;
    };
    networking.allowLan = false;
    # Optional aggregate budget; null retains 25% of host RAM.
    totalMemoryMiB = 12288;
  };
};
```

`diskGiB` is the total initial capacity of one project machine, including its operating system. VM storage is sparse; capacity and allocated host blocks are reported separately. Reflink sharing means allocated-block counts are not exclusive physical usage. Reserve sufficient host free space for actual writes.

Run `sudo tokendrain doctor` to inspect host prerequisites and daemon/helper failures. `tokendrain status` and the Settings page inspect application prerequisites from the daemon and query the authenticated helper for KVM, TUN, Firecracker, `ip`, `nft`, cgroups, IPv4 forwarding, and guest artifacts. They report a failed check if the helper cannot be reached. The daemon's private device namespace and limited executable path remain in place; it does not need direct KVM/TUN access or privileged networking tools. A host `nix` executable is not needed to run the installed service; Nix is provided separately inside the guest.

### Data directory

`services.tokendrain.stateDirectory` defaults to `/var/lib/tokendrain`. To keep the database, encrypted credential store, project machines and application logs on another filesystem:

```nix
services.tokendrain.stateDirectory = "/data/tokendrain";
```

The module creates the directory with service-user ownership, passes it to both daemon and helper, and orders both services after its required mounts. Use a local filesystem supporting ordinary Linux ownership and permissions. The default encryption key stays separately at `/var/lib/tokendrain-keys/master.key`; `masterKeyFile` selects a different runtime key source.

Changing the option does not move existing data. Pause schedules, stop executions and services, copy the complete old state directory with ownership/permissions preserved, then rebuild with the new path. Keep the same encryption key. See [backup and recovery](security.md#recovery-and-backups).

## Privileged boundary

The HTTP daemon runs as `tokendrain`, without Linux capabilities and with `PrivateDevices` enabled. Only `tokendrain-helper` manages TAP/nftables and asks systemd to create transient Firecracker services. The helper listens on `/run/tokendrain/helper.sock`, checks Unix peer credentials, and accepts a versioned fixed set of operations: `start`, `stop`, `list`, read-only `diagnostics`, and read-only `workspace_export`. Callers supply validated execution/project UUIDs and bounded CPU/RAM settings. They cannot supply an arbitrary shell command, host filename, mount or Firecracker configuration. Diagnostics inspect only the helper's fixed platform prerequisites.

Disk opens traverse directory descriptors with `O_NOFOLLOW` and reject symlinks, non-regular files and hardlinks. The helper passes pinned disk descriptors to systemd bind mounts. A Firecracker process runs as `tokendrain-vm` in a systemd root-directory sandbox. It sees its own images/sockets and read-only Nix store, with no application database, encryption key, home directories or other projects. The guest itself receives only its project root and read-only control block devices, never a host directory share. Linux capabilities are removed from the VMM; cgroups bound CPU, memory and processes.

Each VM reserves its configured RAM plus 512 MiB VMM overhead against the aggregate budget. The default budget is 75% of host physical memory. Platform per-VM limits and concurrency are enforced again by the helper, independently of the HTTP scheduler. Do not grant untrusted users access to the helper socket or the daemon's administrative bearer token.

The generated encryption key lives at `/var/lib/tokendrain-keys/master.key` in a root-only directory and reaches the daemon through `LoadCredential`. A private mode-0600 copy in `/run/tokendrain-auth/master-key` accommodates systemd credential mount permissions without weakening the application's key-file checks. With `masterKeyFile`, supply a runtime string path, for example `"/run/secrets/tokendrain-key"`; do not put secret file contents in a Nix path. Back up the long-lived key alongside the application database and project disks.

## Network policy

Every VM has a separate TAP interface named `tdt…` and a unique routed `/30` in `100.127.0.0/16`. There is no shared guest bridge. nftables rules are installed before a TAP is enabled:

- IPv4 public Internet traffic is NATed through the host.
- Host input is dropped, including the host's public addresses and its guest gateway address.
- Traffic between project TAP interfaces is dropped.
- Private, link-local/metadata, carrier-grade NAT, multicast, documentation and reserved address ranges are dropped by default.
- Directly connected host LAN prefixes are discovered across routing tables and blocked, including publicly numbered LANs. A netlink listener refreshes this policy when links, addresses or routes change.
- Guest source-address spoofing and IPv6 are dropped.

DNS defaults to public resolvers `1.1.1.1` and `9.9.9.9`. Setting `networking.allowLan = true` permits LAN/private destinations but continues to block host input and other guests. This is a deliberate expansion of what arbitrary guest software can access.

The module preserves the host's `networking.firewall.filterForward` setting. If forwarding filtering is already enabled, it adds forwarding accepts for VM interfaces while its earlier nftables chain enforces guest denials. It does not add a global forwarding drop policy or trust Tailscale/VPN interfaces. NAT also requires traffic to arrive from a `tdt*` interface, so other traffic using the same address range is unaffected. Existing host VPN routing and upstream firewall policy still determine whether allowed public traffic can reach the Internet.

The module enables nftables, which selects the nftables firewall backend on NixOS unless the host explicitly chooses another backend. Check existing VPN firewall integration when switching from iptables. `networking.firewall.trustedInterfaces` controls access from a VPN to the host; allowing that access is a host policy decision, separate from Tokendrain's VM forwarding rules.

On a normal NixOS nftables reload, empty source sets and a default-drop readiness chain deny guest egress until the helper atomically rebuilds every guard. A periodic two-second reconciliation also restores policy after reloads. Stopping nftables stops its dependent VM services before removing the firewall. Do not delete the table manually or reuse the reserved `tdt` interface prefix.

IPv4 forwarding is enabled with `lib.mkDefault 1`, so a VPN or router module can already define `boot.kernel.sysctl."net.ipv4.ip_forward" = 1` without a duplicate-definition error. Forwarding must remain enabled; an explicit disabling value produces a Tokendrain assertion during evaluation.

## Storage growth and locks

**Project → VM storage → Resize** grows the idle project machine. Tokendrain checks the filesystem before increasing capacity and grows the filesystem in place. Shrinking is unsupported. There are no snapshots, selective restores, or resets.

An asyncio lock and advisory process lock serialize project disk operations. The orchestrator holds that lease throughout execution. The helper separately prevents two VMs from attaching the same project. At daemon startup, VM records and transient systemd services are reconciled before interrupted work is admitted; surviving VMs must be stopped before their disks are changed. A process lock alone cannot protect a disk still attached to a surviving VMM.

Normal shutdown stops Codex, clears runtime credentials and cleanly unmounts the guest disks. On Firecracker this uses `systemctl reboot` with `reboot=k`, which exits the VMM; an ordinary x86 poweroff can leave a halted VMM running. This follows the [Firecracker FAQ](https://github.com/firecracker-microvm/firecracker/blob/main/FAQ.md). The helper waits for shutdown and removes the VM service, TAP and runtime directory. If guest shutdown fails, it applies a bounded forced stop; ext4 then recovers its journal on the next boot. Infrastructure failure must not be represented as successful project completion.

Managed runtime credential files are on tmpfs and are not written into persistent VM storage. Since the agent has root, arbitrary guest software can deliberately copy credentials into persistent files. Runtime-only injection prevents accidental default persistence, not deliberate retention by untrusted software.

## Offline workspace access

While a project is idle, its workspace can be listed, previewed, or exported through the privileged helper. The helper mounts only the selected project's VM filesystem read-only in a private mount namespace; the web daemon has no mount privileges. Workspace-relative paths are validated without following symlinks, and symlinks and special files are excluded from exports. Access is rejected while a VM has the disk attached.

Individual files are downloaded directly; folders and the whole workspace are exported as ZIP. Text/source, sanitized Markdown, and browser-supported images can be previewed. Automatic previews are bounded to 2 MiB, explicit larger previews to 32 MiB; larger files remain download-only. Archives are written through the export path without loading the entire workspace into daemon memory.

Workspace exports are restricted to `/workspace`; the remainder of the VM filesystem is never exposed by the Workspace API.

## Validation and debugging

Build platform artifacts:

```sh
nix build .#tokendrain .#tokendrain-guestd .#guest-artifacts
nix develop
uv sync --group dev
pytest -m 'not kvm and not nix'
```

The opt-in real KVM test requires no root and spends no provider usage. It boots the actual Nix guest twice, controls the actual Codex app-server through vsock with a test-only runtime token, executes local `command/exec` requests, and checks workspace/home persistence plus runtime-file removal:

```sh
export TOKENDRAIN_TEST_GUEST_ARTIFACTS="$(nix build .#guest-artifacts --no-link --print-out-paths)"
nix develop -c .venv/bin/pytest tests/integration/test_kvm_guest.py -m kvm -v
```

The NixOS tests run in disposable test machines:

```sh
nix build .#checks.x86_64-linux.module -L
nix build .#checks.x86_64-linux.firecracker -L
```

The Firecracker test requires nested KVM. It verifies current control A→B on the same project root, persistence of ordinary machine files and a Nix-installed tool, temporary credentials, storage growth, idle Workspace browsing, and active Workspace exclusion. It also exercises the privileged helper/systemd sandbox, real guest boot/control, concurrent project networking, allowed public egress, host/LAN/interguest denial, fail-closed firewall reloads, disk persistence and cleanup. Its simulated public endpoint is inside the isolated test network, so these checks do not depend on a public website or real credentials.

On an installed host:

```sh
journalctl -u tokendraind -u tokendrain-helper -f
journalctl -u 'tokendrain-vm-*' --since today
systemctl list-units 'tokendrain-vm-*'
nft list table inet tokendrain
ip link show
```

Run `sudo tokendrain doctor` for host diagnostics and `sudo tokendrain login-token` to retrieve the browser's administrative login token. `tokendrain status`, `projects` and `runs` use the protected token file; run them as an account allowed to read it. Use `--json` before the command for structured output. The module publishes non-secret paths/defaults in `/etc/tokendrain/platform.json`; environment variables and explicit CLI options can select another deployment.

The backend records execution/project IDs in helper logs. Firecracker console output appears in the corresponding transient service's journal. Console output is untrusted guest output and may contain values printed by software inside the VM; restrict access and retention accordingly.

The current implementation follows the official [Firecracker configuration API](https://github.com/firecracker-microvm/firecracker/blob/main/src/firecracker/swagger/firecracker.yaml) and [vsock protocol](https://github.com/firecracker-microvm/firecracker/blob/main/docs/vsock.md). Nix builds the base filesystem and boot artifacts; the helper launches Firecracker directly. Dependency revisions are recorded in `flake.lock`.
