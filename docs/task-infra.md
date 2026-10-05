# Infrastructure implementation

The project storage format is one persistent Linux root filesystem. Nix produces a base machine once; project creation clones it with reflink/sparse copying and grows it to total configured capacity. Project userspace and Nix store/database remain independent of host Tokendrain upgrades.

Each boot attaches the current read-only control payload. Its stable activation entrypoint imports missing guestd/Codex closure paths through a local Nix cache, registers them in the persistent store, protects the current control roots, and starts current guestd with an explicit managed Codex path. Managed credentials remain on `/run` tmpfs.

There is no storage conversion from older installations, directory persistence list, store overlay, reset, or snapshot workflow. Idle VM storage supports growth; Workspace exports mount the project filesystem read-only and expose only `/workspace`.

## Security boundaries

- The authenticated privileged helper accepts resource IDs rather than arbitrary host paths.
- Disk descriptors are pinned with no-follow traversal and regular-file/hardlink checks.
- Firecracker runs under a dedicated user in a systemd filesystem/cgroup sandbox.
- The guest sees block devices, no host directory share.
- Host input, inter-project traffic, source spoofing, IPv6, and default LAN access remain blocked.
- Firewall reloads remain fail-closed until policy refresh, and stopping nftables stops dependent VMMs.
- Leases and helper records exclude concurrent writable attachment and active Workspace export.
- Bounded host teardown remains independent of guest cooperation.

## Focused validation

`tests/integration/test_storage_real.py` exercises actual filesystem cloning, checking, growth, preservation, and shrink rejection without root.

`tests/integration/test_kvm_guest.py` boots the same root disk twice, checks files across ordinary machine locations, a Nix-installed tool/store registration, managed runtime credentials, and optional A→B control upgrade artifacts.

`nix/tests/firecracker.nix` retains the real helper, network isolation, reload, concurrency and shutdown checks. Its guest test also upgrades control on the same machine, browses `/workspace` offline, rejects active access, and grows storage.

See [microVM operations](microvms.md) and [development](development.md) for commands.
