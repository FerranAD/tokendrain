# MicroVMs and project storage

Tokendrain boots a fresh Firecracker VM for each project execution. The machine is disposable; the two project disks are durable. Creating a project, running it, taking snapshots, or changing its task log never requires `nixos-rebuild`.

## Immutable platform, persistent development state

The flake uses [microvm.nix](https://github.com/microvm-nix/microvm.nix) to build the NixOS kernel, initial RAM filesystem and immutable EROFS Nix-store image. Tokendrain generates the Firecracker configuration at runtime, so projects are not declared in NixOS configuration.

| Domain | Guest location | Retained across runs |
| --- | --- | --- |
| Immutable guest | kernel, initrd, lower `/nix/store` | Replaced when the platform is upgraded |
| Environment disk | `/persist` | Root home, Nix profiles/database, writable Nix-store overlay, `/var/lib`, `/var/cache`, Codex threads and caches |
| Workspace disk | `/workspace` | Source tree, Git repositories and project data |
| Runtime state | `/run/tokendrain` | Discarded when the VM stops |

The root filesystem is tmpfs. `/root`, `/nix/var`, `/var/lib` and `/var/cache` are bound to directories on the environment disk. A writable overlay combines installed Nix packages with the immutable base. Agent changes to other parts of the ephemeral root filesystem, such as an arbitrary `/etc` edit, do not persist. Store persistent tools/configuration in the home directory, Nix profiles or `/persist`.

The guest includes Git, curl, jq, Nix, Python, Node, Rustup, a C/C++ compiler, CMake, Make, GitHub CLI and Codex. It can install additional tools without a host rebuild. The agent has root authority inside its VM. There is no SSH control path and no command approval dialog.

A platform upgrade changes the immutable lower Nix store. Before an upgrade, stop executions and retain snapshots. If an old persistent Nix overlay no longer works with the new base, reset the environment and retain the workspace. Tokendrain does not use Firecracker RAM snapshots for durable storage.

## Host requirements and installation

Use Linux NixOS on x86-64 or aarch64, with hardware virtualization exposed to the host and enough RAM for the selected concurrency. Firecracker needs read/write access to `/dev/kvm`; TAP networking needs `/dev/net/tun`. Nested virtualization must be enabled if the NixOS host is itself virtualized. The module loads KVM/TUN and adds its dedicated VMM user to `kvm`; firmware or an outer hypervisor may still need configuration.

```nix
services.tokendrain = {
  enable = true;
  web.listenAddress = "127.0.0.1";
  web.port = 8742;
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

`diskGiB` is the initial size of **each** disk. Images are sparse; logical disk size and allocated host blocks are reported separately. Reflink sharing means allocated-block counts are not exclusive physical usage. Reserve sufficient host free space for actual writes and non-reflink snapshot copies.

Run `tokendrain doctor` to inspect prerequisites. The daemon uses a restricted device namespace; helper status and host-side diagnostics are more useful than assuming the daemon's own `/dev` is the host's `/dev`.

## Privileged boundary

The HTTP daemon runs as `tokendrain`, without Linux capabilities. Only `tokendrain-helper` manages TAP/nftables and asks systemd to create transient Firecracker services. The helper listens on `/run/tokendrain/helper.sock`, checks Unix peer credentials, and accepts a versioned fixed set of operations: `start`, `stop`, and `list`. Callers supply validated execution/project UUIDs and bounded CPU/RAM settings. They cannot supply an arbitrary shell command, host filename, mount or Firecracker configuration.

Disk opens traverse directory descriptors with `O_NOFOLLOW` and reject symlinks, non-regular files and hardlinks. The helper passes pinned disk descriptors to systemd bind mounts. A Firecracker process runs as `tokendrain-vm` in a systemd root-directory sandbox. It sees its own images/sockets and read-only Nix store, with no application database, encryption key, home directories or other projects. The guest itself receives only its immutable store image, never a host directory share. Linux capabilities are removed from the VMM; cgroups bound CPU, memory and processes.

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

The module integrates forwarding accepts with the NixOS firewall while its earlier nftables chain enforces denials. On a normal NixOS nftables reload, empty source sets and a default-drop readiness chain deny guest egress until the helper atomically rebuilds every guard. A periodic two-second reconciliation also restores policy after reloads. Stopping nftables stops its dependent VM services before removing the firewall. Do not delete the table manually or reuse the reserved `tdt` interface prefix. Existing host VPN routing and upstream firewall policy still determine whether allowed public traffic can reach the Internet.

IPv4 forwarding is enabled with `lib.mkDefault 1`, so a VPN or router module can already define `boot.kernel.sysctl."net.ipv4.ip_forward" = 1` without a duplicate-definition error. Forwarding must remain enabled; an explicit disabling value produces a Tokendrain assertion during evaluation.

## Snapshots, restores and locks

A pre-run snapshot copies both offline images using `cp --reflink=auto --sparse=always`. Filesystems with reflinks share unchanged blocks; other filesystems get a sparse ordinary copy. A snapshot is published only after both copies and metadata are complete. Insufficient space or a failed copy leaves no partially published snapshot.

Restore supports `workspace`, `environment` or `all`. Replacement files are prepared before a small durable restore journal is committed. If the daemon stops during replacement, the next storage lease rolls the operation forward. Environment reset creates a new ext4 environment image. Disk growth runs an offline filesystem check and `resize2fs`; shrinking is intentionally unsupported.

An asyncio lock and advisory process lock serialize project disk operations. The orchestrator holds that lease throughout execution. The helper separately prevents two VMs from attaching the same project. At daemon startup, VM records and transient systemd services are reconciled before interrupted work is admitted; surviving VMs must be stopped before their disks are changed. A process lock alone cannot protect a disk still attached to a surviving VMM.

Normal shutdown stops Codex, clears runtime credentials and cleanly unmounts the guest disks. On Firecracker this uses `systemctl reboot` with `reboot=k`, which exits the VMM; an ordinary x86 poweroff can leave a halted VMM running. This follows the [Firecracker FAQ](https://github.com/firecracker-microvm/firecracker/blob/main/FAQ.md). The helper waits for shutdown and removes the VM service, TAP and runtime directory. If guest shutdown fails, it applies a bounded forced stop; ext4 then recovers its journal on the next boot. Infrastructure failure must not be represented as successful project completion.

Runtime credential files are on tmpfs and therefore are not copied by host disk snapshots. Since the agent has root, arbitrary guest software can deliberately copy credentials into persistent files. Runtime-only injection prevents accidental default persistence, not deliberate retention by untrusted software.

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

The Firecracker test requires nested KVM. It exercises the privileged helper/systemd sandbox, real guest boot/control, concurrent project networking, public-egress allowance, host/LAN/interguest denial, fail-closed firewall reloads, disk persistence and cleanup. Its simulated public endpoint is inside the isolated test network, so these checks do not depend on a public website or real credentials.

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

The current implementation follows the official [Firecracker configuration API](https://github.com/firecracker-microvm/firecracker/blob/main/src/firecracker/swagger/firecracker.yaml), [vsock protocol](https://github.com/firecracker-microvm/firecracker/blob/main/docs/vsock.md), and [microvm.nix Firecracker runner](https://github.com/microvm-nix/microvm.nix/blob/main/lib/runners/firecracker.nix). Dependency revisions are recorded in `flake.lock`.
