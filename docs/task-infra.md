# Infrastructure task log

- 2026-10-02: inspected NixOS host; Nix available, /dev/kvm and /dev/net/tun absent. Real VM tests require those host facilities.
- Read current upstream microvm.nix Firecracker runner, volume/store-overlay modules and official Firecracker API/vsock docs.
- Decision: use microvm.nix for immutable guest closure/kernel/initrd/store image; host dynamically attaches persistent ext4 environment/workspace disks. A small privileged Unix-socket helper manages networking/systemd; HTTP daemon remains unprivileged.

## Checkpoint for requested Codex restart

Implemented (not yet exercised on real KVM):

- `flake.nix`, `flake.lock`, `nix/package.nix`, `nix/web.nix`: pinned nixpkgs `c59305b` and microvm.nix `3f1540f`; packages `tokendrain`, `tokendrain-guestd`, `guest-artifacts`, web; dev shell; NixOS service smoke test derivation.
- `nix/guest.nix`, `nix/guest-artifacts.nix`: microvm.nix builds immutable kernel/initrd/store. ext4 environment `/persist` and workspace `/workspace`; persistent root home, Nix state, caches; writable Nix store overlay; Python guestd vsock port 4050; Codex/development tools.
- `modules/tokendrain.nix`: unprivileged HTTP daemon, protected runtime master key (systemd LoadCredential), separate privileged helper, separate Firecracker user, KVM/TUN/network prerequisites, early nftables host/LAN/interguest restrictions, resource defaults/caps, runtime directories.
- `src/tokendrain/vm/commands.py`: centralized async subprocess interface with timeout and process cleanup.
- `src/tokendrain/vm/{models,firecracker,helper}.py`: typed backend and Unix-socket helper protocol. Peer UID authentication; accepts UUID/resource operations only; no arbitrary shell/path operations. Dynamic Firecracker config, systemd root-directory sandbox and cgroup CPU/RAM limits, safe disk opens with O_NOFOLLOW dir traversal, idempotent cleanup and service reconciliation. Aggregate memory reservations protect a quarter of host RAM by default.
- `src/tokendrain/networking/linux.py`: isolated routed TAP per VM, /30 allocations, anti-spoof nftables rules, connected LAN route set refresh, cleanup on failed creation.
- `src/tokendrain/storage/files.py`: separate sparse ext4 disks; reflink snapshots with sparse copy fallback; durable metadata; domain restore with roll-forward journal; environment reset; offline disk grow; independent usage; delete; async + process flock leases reentrant within one task.
- `tests/unit/test_infrastructure.py`: ten tests for snapshot/domain restore, crash journal recovery, concurrency/process locks, snapshot failure cleanup, usage/deletion, input validation, isolation rules, TAP cleanup, helper client request/response.

Validation at stop:

- Infrastructure Python Ruff passes.
- Infrastructure Python strict mypy passes (`--follow-imports=silent`).
- Ten infrastructure unit tests pass.
- `XDG_CACHE_HOME=/tmp/tokendrain-nix-cache nix flake lock path:/home/ferran/tokendrain` succeeded.
- `nix eval path:/home/ferran/tokendrain#packages.x86_64-linux.guest-artifacts.drvPath` succeeded.
- Module test derivation evaluation reached frontend package evaluation and stopped because `web/package-lock.json` did not yet exist. Retry after frontend agent has generated it.
- No package builds, real guest boots, actual nft/systemd helper operations or persistence boots have yet been exercised.
- /dev/kvm and /dev/net/tun were still invisible in the pre-restart Codex sandbox; user has now enabled them on host and requested restart.

Exact next actions:

1. Reinspect `/dev/kvm`, `/dev/net/tun`, groups and actual KVM open ioctl after new Codex session.
2. Complete Nix package/module evaluation after package-lock exists, `nix fmt`, package builds. Current flake smoke test expects `/api/v1/system/status`; coordinate with final root API routes. All Nix commands need `XDG_CACHE_HOME=/tmp/tokendrain-nix-cache` in this sandbox.
3. Real helper/Firecracker boot test: verify systemd `RootDirectory` bind mounts, runtime socket permissions, TAP ownership, guest IP configuration, control connection, graceful shutdown and persistent environment/workspace across two boots.
4. Add real KVM test harness/NixOS nested KVM test for vsock and persistence; test default networking denies host/LAN/other guests and permits public HTTP/DNS.
5. Add meaningful command-backend integration tests with actual mkfs/e2fsck/resize2fs/sparse cp and dynamic LAN-route update test (current storage fixtures intentionally use tiny byte files and fake runner).
6. Audit shutdown timing: backend stop currently systemctl SIGTERM after guestd graceful shutdown from root; caller must request guest shutdown first, then stop backend. Infrastructure failure must never be reported as completed.
7. Root must call backend.reconcile(), stop all surviving handles before accepting mutations/requeued work, and hold storage.lease throughout each execution. Process flocks alone do not guard disks still attached to a surviving systemd VM after daemon death.
8. Write required `docs/microvms.md` with immutable base/persistent domains, network ranges/DNS, offline snapshots, helper boundary, resource caps, commands and KVM testing. Other architectural/security docs are root-owned.
9. Review limitations: fixed guest DNS 1.1.1.1/9.9.9.9; connected LAN set refresh occurs before each VM boot (host network reconfiguration during a run needs policy refresh); helper should expose platform status for doctor because daemon PrivateDevices hides KVM; whole immutable /nix/store is read-only in VMM process jail (never mounted into guest); arbitrary guest software can deliberately persist runtime secret values despite runtime-only injection.
10. Ensure final Python packaging includes migration .mako files (hatch packaging uses src directory), frontend static asset build, scripts, and daemon Settings names match module.

No commits performed by infrastructure agent; root will create the requested checkpoint commit.

## Resumed implementation and real infrastructure validation

The earlier checkpoint above records the pre-restart environment. KVM and TUN
became available after restarting Codex; KVM API version 12 and VM creation were
verified without changing the host configuration. Sudo remains password-gated;
privileged tests run in disposable NixOS test machines instead of modifying the
user's host.

Implemented and exercised since that checkpoint:

- Built the Python applications, production frontend and microvm.nix guest
  kernel/initrd/EROFS image from the pinned flake. Both x86-64 and aarch64 guest
  and test derivations evaluate. Only x86-64 was actually booted here.
- Real unprivileged Firecracker test boots the production guest twice and uses
  the actual Codex app-server `command/exec` API over vsock. Workspace, root home,
  Nix store additions and Nix validity database survive; `/run/tokendrain` does
  not. This test spends no provider usage and uses no real account credentials.
- A nested-KVM NixOS test exercises the installed privileged helper and actual
  transient systemd Firecracker services, including their separate user,
  root-directory mounts, PID namespace, resource limits and Unix control paths.
  Two concurrent guests can access a simulated public HTTP endpoint while host,
  private LAN, publicly numbered LAN and other guest endpoints are denied.
- Firewall reload starts with empty source guards and a drop readiness chain.
  The helper atomically restores source-address and LAN sets before enabling
  egress; netlink updates track all routing tables and a periodic reconciliation
  handles nftables reloads. VM services depend on nftables, so stopping the
  firewall terminates VMMs before rules are removed. The tests cover live route
  additions/removals, helper restarts, reloads and dead-VM cleanup.
- Normal shutdown now drains the guest transport and requests Firecracker's
  documented reboot-based exit. The direct test and nested test verify clean
  exit; stalled VMMs are killed after a bounded grace period. See guest-agent
  commit 204887e for the guest shutdown corrections found by these tests.
- The helper reports systemd readiness only after its socket and initial policy
  are ready. Expected SIGTERM exits cleanly; route-watcher failures fail closed.
- Independent review fixed disk mutation cancellation races, restore journal
  fsync ordering, durable project disk publication and network cleanup errors.
  Mutation executor futures retain leases until completion even during repeated
  cancellation or event-loop shutdown. Disk descriptors remain pinned through
  systemd mount setup and paths cannot traverse symlinks or hardlinks.
- Generated encryption keys now live in a root-only directory outside the
  daemon-owned application state, reach the daemon through LoadCredential, and
  are flushed before publication. Host Codex matches the pinned guest version.
- Added docs/microvms.md with deployment, persistence, security boundary,
  networking, resource control and real test commands.

Validation records:

- Ruff and strict mypy pass for infrastructure modules.
- 17 focused infrastructure tests pass, including actual ext4 formatting,
  reflink/copy snapshots, debugfs sentinel restoration and offline disk growth.
- Real two-boot guest/Nix-state persistence test passed in 17.88 seconds.
- Full nested NixOS helper/network/persistence/reload test passed in 115.59
  seconds before adding the final live-route/firewall-stop recovery assertions.
- The final recovery assertions exposed and fixed systemd helper readiness.
  The complete nested NixOS test then passed in 120.05 seconds, including live
  route changes, fail-closed reload, dependent VM termination before nftables
  stops, helper restart and stale-record reconciliation.
- Nix package checks passed: 41 tests passed, one unavailable-local-Codex test
  skipped and the opt-in KVM test deselected (run separately above).

Remaining cross-component validation:

- The separate NixOS HTTP/module smoke test requires the root agent's daemon
  entrypoint (`tokendrain.cli.daemon`). At this checkpoint that file was still
  being implemented; the helper test deliberately disables only the HTTP
  daemon so infrastructure can be validated independently.
- aarch64 artifacts evaluate but require an aarch64 KVM machine for real boot
  validation. Live OpenAI/GitHub credentials are not needed or used in these
  infrastructure tests; provider-authenticated autonomous runs need their own
  end-to-end acceptance test.
