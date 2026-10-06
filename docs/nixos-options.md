# NixOS module options

All options below are under `services.tokendrain`. Import `tokendrain.nixosModules.tokendrain` from the flake as shown in the [installation guide](getting-started.md).

## Service and credentials

| Option | Type | Default | What it controls |
| --- | --- | --- | --- |
| `enable` | Boolean | `false` | Enable the daemon, privileged VM helper, service users, guest networking, and systemd units. |
| `package` | Package | Flake's `packages.<system>.tokendrain` | Application package, including the compiled website. The bare module has no default; the flake module supplies it. |
| `codexPackage` | Package | Flake's pinned `nixpkgs` Codex package | Host `codex app-server` used for authentication and usage probes. The bare module defaults to `pkgs.codex`. Guest Codex comes from the guest artifacts. |
| `claudeCodePackage` | Package | Flake's pinned `nixpkgs` Claude Code package | Host `claude auth login` for browser-assisted subscription sign-in. Guest Claude Code comes from the control bundle. The flake allows this unfree package; a bare-module installation must allow `claude-code` explicitly. |
| `stateDirectory` | String | `"/var/lib/tokendrain"` | Database, encrypted credentials, and persistent project machines. Must be an absolute path without spaces. Changing it does not move existing data. |
| `masterKeyFile` | Null or string | `null` | Runtime secret path containing a 32-byte encryption key, or its base64 encoding. With `null`, generate a protected key at `/var/lib/tokendrain-keys/master.key` on first start. |
| `auth.mode` | `"token"` or `"none"` | `"token"` | Require the application's admin-token login, or rely on private-network / external authentication. |
| `auth.adminTokenFile` | Null or string | `null` | Runtime file containing an admin token of at least 32 characters. Required when `auth.mode = "token"`. Supports agenix and sops-nix paths. |
| `concurrency` | Integer, 1–64 | `2` | Host ceiling for simultaneous project executions. Website settings can select a lower concurrency. |

Secret options take runtime **strings**, such as `"/run/secrets/tokendrain-admin"`, rather than Nix path literals that copy secrets into the Nix store. The module supplies credentials to the daemon through systemd and protected runtime files. Back up the master key with your data; see [security and backups](security.md).

## Web interface

| Option | Type | Default | What it controls |
| --- | --- | --- | --- |
| `web.listenAddress` | String | `"127.0.0.1"` | Address the web daemon binds to. The module does not open a public firewall port. |
| `web.port` | NixOS port | `8742` | Web UI and API TCP port. Use 1–65535; the daemon rejects port 0. |
| `web.publicUrl` | String | Derived from listen address and port | Browser-facing URL for origin checks, GitHub setup, and notification links. With default settings: `"http://127.0.0.1:8742"`. |

The derived public URL uses `127.0.0.1` when listening on `0.0.0.0` or `::`, and brackets a specific IPv6 address. Set it explicitly when accessing the app through a hostname or HTTPS reverse proxy:

```nix
services.tokendrain.web = {
  listenAddress = "127.0.0.1";
  port = 8742;
  publicUrl = "https://tokendrain.example.com";
};
```

`publicUrl` does not configure the proxy, TLS, or firewall. Keep the listener private when another service handles authentication.

## VM backend and host limits

| Option | Type | Default | What it controls |
| --- | --- | --- | --- |
| `microvm.hypervisor` | `"firecracker"` | `"firecracker"` | VM backend; Firecracker is the only supported value. |
| `microvm.guestArtifacts` | Package | Flake's `packages.<system>.guest-artifacts` | Kernel, initrd, base project machine, and current control bundle. The bare module has no default; the flake module supplies it. |
| `microvm.maxMemoryMiB` | Integer, 512–131072 | `16384` | Maximum guest RAM per VM, enforced by the privileged helper. |
| `microvm.maxVcpus` | Integer, 1–32 | `16` | Maximum vCPUs per VM, enforced by the privileged helper. |
| `microvm.totalMemoryMiB` | Null or integer, 512–16777216 | `null` | Aggregate VM RAM budget, including 512 MiB of VMM overhead per guest. With `null`, use at most 75% of physical host RAM. |
| `microvm.networking.allowLan` | Boolean | `false` | Allow guest access to private/LAN ranges. Host access and access to other guests remain blocked. |

The helper checks both concurrency and memory budgets before admitting a VM. For example, two 4096 MiB guests reserve `2 × (4096 + 512) = 9216 MiB`, regardless of how much RAM they happen to be using.

The module loads KVM/TUN, enables nftables and IPv4 forwarding, and installs guest isolation/NAT rules. An explicit host configuration disabling IPv4 forwarding fails an assertion. Hardware or nested virtualization must still expose `/dev/kvm`; see [MicroVM operations](microvms.md).

## Initial VM defaults

| Option | Type | Default | What it controls |
| --- | --- | --- | --- |
| `microvm.defaults.vcpus` | Integer, 1–32 | `4` | Initial vCPUs per VM. Must not exceed `microvm.maxVcpus`. |
| `microvm.defaults.memoryMiB` | Integer, 512–131072 | `4096` | Initial guest RAM per VM. Must not exceed `microvm.maxMemoryMiB`. |
| `microvm.defaults.diskGiB` | Integer, 1–4096 | `40` | Persistent storage capacity for newly created project machines. |

Saved **Settings → Execution defaults** values take precedence over these initial values. Concurrency, vCPUs, and memory remain bounded by the host ceilings above. Changing storage defaults affects new project machines; resize existing machines through the project's VM storage settings.

```nix
services.tokendrain = {
  concurrency = 3;
  microvm = {
    maxVcpus = 8;
    maxMemoryMiB = 8192;
    totalMemoryMiB = 16384;
    defaults = {
      vcpus = 4;
      memoryMiB = 4096;
      diskGiB = 60;
    };
  };
};
```

These are all 21 options exported by the module. Project tasks, models, GitHub bindings, usage stop thresholds, cron schedules, automations, and ntfy destinations are configured in the website rather than through NixOS options.
