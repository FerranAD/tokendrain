{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    mkEnableOption
    mkOption
    types
    mkIf
    ;
  cfg = config.services.tokendrain;
  helperConfig = pkgs.writeText "tokendrain-helper.json" (
    builtins.toJSON {
      state_dir = cfg.stateDirectory;
      runtime_dir = "/run/tokendrain-vms";
      socket_path = "/run/tokendrain/helper.sock";
      guest_artifacts = toString cfg.microvm.guestArtifacts;
      firecracker = "${pkgs.firecracker}/bin/firecracker";
      max_concurrency = cfg.concurrency;
      max_memory_mib = cfg.microvm.maxMemoryMiB;
      max_vcpus = cfg.microvm.maxVcpus;
      total_memory_mib = cfg.microvm.totalMemoryMiB;
    }
  );
  blocked = "0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.0.2.0/24, 192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24, 224.0.0.0/4, 240.0.0.0/4";
in
{
  options.services.tokendrain = {
    enable = mkEnableOption "autonomous Codex projects in isolated Firecracker microVMs";
    package = mkOption {
      type = types.package;
      description = "Tokendrain application package.";
    };
    codexPackage = mkOption {
      type = types.package;
      default = pkgs.codex;
      description = "Host Codex app-server used for authentication and usage probes.";
    };
    stateDirectory = mkOption {
      type = types.str;
      default = "/var/lib/tokendrain";
      description = "Absolute directory for the database, encrypted credentials, project machines, and logs. Existing data is not moved automatically.";
    };
    masterKeyFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Runtime path to a 32-byte master key (or base64 encoding). Never use a Nix path containing a secret. With null, generate a protected local key on first start.";
    };
    auth = {
      mode = mkOption {
        type = types.enum [
          "token"
          "none"
        ];
        default = "token";
        description = "Administrative token login, or external/private-network access control.";
      };
      adminTokenFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Runtime secret file containing an admin token (at least 32 characters). Supports agenix/sops-nix.";
      };
    };
    concurrency = mkOption {
      type = types.ints.between 1 64;
      default = 2;
    };
    web = {
      listenAddress = mkOption {
        type = types.str;
        default = "127.0.0.1";
      };
      port = mkOption {
        type = types.port;
        default = 8742;
      };
      publicUrl = mkOption {
        type = types.str;
        default = "http://${
          if
            builtins.elem cfg.web.listenAddress [
              "0.0.0.0"
              "::"
            ]
          then
            "127.0.0.1"
          else if lib.hasInfix ":" cfg.web.listenAddress then
            "[${cfg.web.listenAddress}]"
          else
            cfg.web.listenAddress
        }:${toString cfg.web.port}";
        description = "Browser-facing URL, used for origin checks behind a reverse proxy.";
      };
    };
    microvm = {
      hypervisor = mkOption {
        type = types.enum [ "firecracker" ];
        default = "firecracker";
      };
      guestArtifacts = mkOption {
        type = types.package;
        description = "Nix-built kernel/initrd, base project machine, and current control bundle.";
      };
      maxMemoryMiB = mkOption {
        type = types.ints.between 512 131072;
        default = 16384;
      };
      totalMemoryMiB = mkOption {
        type = types.nullOr (types.ints.between 512 16777216);
        default = null;
        description = "Aggregate VM RAM budget including 512 MiB VMM overhead per guest. Null reserves 25% of host RAM.";
      };
      maxVcpus = mkOption {
        type = types.ints.between 1 32;
        default = 16;
      };
      defaults = {
        vcpus = mkOption {
          type = types.ints.between 1 32;
          default = 4;
        };
        memoryMiB = mkOption {
          type = types.ints.between 512 131072;
          default = 4096;
        };
        diskGiB = mkOption {
          type = types.ints.between 1 4096;
          default = 40;
          description = "Total persistent storage capacity of each new project VM in GiB.";
        };
      };
      networking.allowLan = mkOption {
        type = types.bool;
        default = false;
        description = "Allow guest access to private/LAN address ranges. Host and other guests remain blocked.";
      };
    };
  };
  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.auth.mode != "token" || cfg.auth.adminTokenFile != null;
        message = "Tokendrain auth.mode=token requires auth.adminTokenFile (a runtime secret path).";
      }
      {
        assertion = cfg.microvm.defaults.memoryMiB <= cfg.microvm.maxMemoryMiB;
        message = "Tokendrain default memory must not exceed microvm.maxMemoryMiB.";
      }
      {
        assertion = cfg.microvm.defaults.vcpus <= cfg.microvm.maxVcpus;
        message = "Tokendrain default vCPUs must not exceed microvm.maxVcpus.";
      }
      {
        assertion = lib.hasPrefix "/" cfg.stateDirectory && !(lib.hasInfix " " cfg.stateDirectory);
        message = "Tokendrain stateDirectory must be an absolute path without spaces.";
      }
      {
        assertion = builtins.elem config.boot.kernel.sysctl."net.ipv4.ip_forward" [
          1
          "1"
          true
        ];
        message = "Tokendrain requires boot.kernel.sysctl.\"net.ipv4.ip_forward\" to be enabled for guest networking.";
      }
    ];
    users.groups.tokendrain = { };
    users.groups.tokendrain-vm = { };
    users.users.tokendrain = {
      isSystemUser = true;
      group = "tokendrain";
      extraGroups = [ "tokendrain-vm" ];
      home = cfg.stateDirectory;
    };
    users.users.tokendrain-vm = {
      isSystemUser = true;
      group = "tokendrain-vm";
      extraGroups = [ "kvm" ];
    };
    boot.kernelModules = [
      "kvm"
      "tun"
    ];
    # sysctl values use mergeOneOption, so even two ordinary definitions of 1
    # conflict. Allow an existing VPN/router configuration to supply this value.
    boot.kernel.sysctl."net.ipv4.ip_forward" = lib.mkDefault 1;
    networking.nftables.enable = true;
    networking.nftables.tables.tokendrain = {
      family = "inet";
      content = ''
        set lan4 { type ipv4_addr; flags interval; auto-merge; }
        set guest_sources { type ifname . ipv4_addr; }
        chain source_guard {
          type filter hook prerouting priority -310; policy accept;
          iifname "tdt*" iifname . ip saddr @guest_sources return
          iifname "tdt*" drop
        }
        chain routing_ready { drop; }
        chain host_guard {
          type filter hook input priority -20; policy accept;
          iifname "tdt*" drop
        }
        chain guests {
          type filter hook forward priority -20; policy accept;
          iifname "tdt*" oifname "tdt*" drop
          iifname "tdt*" meta nfproto ipv6 drop
          oifname "tdt*" meta nfproto ipv6 drop
          iifname "tdt*" fib daddr type local drop
          iifname "tdt*" jump egress
          oifname "tdt*" ct state established,related accept
          oifname "tdt*" drop
        }
        chain egress {
          jump routing_ready
          ${lib.optionalString (
            !cfg.microvm.networking.allowLan
          ) "ip daddr @lan4 drop\nip daddr { ${blocked} } drop"}
        }
        chain nat {
          type nat hook postrouting priority srcnat; policy accept;
          ip saddr 100.127.0.0/16 oifname != "tdt*" masquerade
        }
      '';
    };
    networking.firewall.filterForward = true;
    networking.firewall.extraForwardRules = ''
      iifname "tdt*" accept
      oifname "tdt*" ct state established,related accept
    '';
    environment.systemPackages = [
      cfg.package
      cfg.codexPackage
      pkgs.firecracker
      pkgs.iproute2
      pkgs.nftables
      pkgs.e2fsprogs
    ];
    # Public paths/defaults let the administrative CLI diagnose this deployment
    # outside the daemon's environment. This file contains no secret values.
    environment.etc."tokendrain/platform.json".text = builtins.toJSON {
      state_dir = cfg.stateDirectory;
      master_key_file =
        if cfg.masterKeyFile == null then "/var/lib/tokendrain-keys/master.key" else cfg.masterKeyFile;
      guest_artifacts = toString cfg.microvm.guestArtifacts;
      helper_socket = "/run/tokendrain/helper.sock";
      auth_mode = cfg.auth.mode;
      admin_token_file = if cfg.auth.mode == "token" then "/run/tokendrain-auth/admin-token" else null;
      listen_address = cfg.web.listenAddress;
      port = cfg.web.port;
    };
    systemd.tmpfiles.rules = [
      "d ${cfg.stateDirectory} 0700 tokendrain tokendrain -"
      "d /var/lib/tokendrain-keys 0700 root root -"
      "d /run/tokendrain 0755 root root -"
      "d /run/tokendrain-vms 0755 root root -"
    ];
    systemd.services.tokendrain-key = mkIf (cfg.masterKeyFile == null) {
      description = "Initialize protected Tokendrain encryption key";
      before = [ "tokendraind.service" ];
      requiredBy = [ "tokendraind.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        UMask = "0077";
      };
      script = ''
        if ! test -e ${lib.escapeShellArg "/var/lib/tokendrain-keys/master.key"}; then
          ${pkgs.coreutils}/bin/head -c 32 /dev/urandom > ${lib.escapeShellArg "/var/lib/tokendrain-keys/master.key.new"}
          chmod 0600 ${lib.escapeShellArg "/var/lib/tokendrain-keys/master.key.new"}
          chown root:root ${lib.escapeShellArg "/var/lib/tokendrain-keys/master.key.new"}
          ${pkgs.coreutils}/bin/sync -f ${lib.escapeShellArg "/var/lib/tokendrain-keys/master.key.new"}
          mv ${lib.escapeShellArg "/var/lib/tokendrain-keys/master.key.new"} ${lib.escapeShellArg "/var/lib/tokendrain-keys/master.key"}
          ${pkgs.coreutils}/bin/sync -f ${lib.escapeShellArg "/var/lib/tokendrain-keys"}
        fi
      '';
    };
    systemd.services.tokendrain-helper = {
      description = "Tokendrain privileged VM/network helper";
      unitConfig.RequiresMountsFor = [ cfg.stateDirectory ];
      wantedBy = [ "multi-user.target" ];
      after = [
        "network.target"
        "nftables.service"
      ];
      requires = [ "nftables.service" ];
      path = [
        pkgs.systemd
        pkgs.iproute2
        pkgs.nftables
        pkgs.util-linux
        pkgs.coreutils
      ];
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/tokendrain-helper --config ${helperConfig}";
        Type = "notify";
        NotifyAccess = "main";
        TimeoutStartSec = 30;
        Restart = "on-failure";
        RestartSec = 2;
        UMask = "0077";
        ProtectHome = true;
        ProtectSystem = "strict";
        ReadWritePaths = [
          cfg.stateDirectory
          "/run/tokendrain"
          "/run/tokendrain-vms"
        ];
        PrivateTmp = true;
        NoNewPrivileges = true;
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_NETLINK"
          "AF_INET"
        ];
        CapabilityBoundingSet = [
          "CAP_SYS_ADMIN" # Read-only exports in a private mount namespace.
          "CAP_NET_ADMIN"
          "CAP_CHOWN"
          "CAP_DAC_OVERRIDE"
          "CAP_FOWNER"
        ];
      };
    };
    systemd.services.tokendraind = {
      description = "Tokendrain project/run service";
      unitConfig.RequiresMountsFor = [ cfg.stateDirectory ];
      wantedBy = [ "multi-user.target" ];
      after = [
        "network-online.target"
        "tokendrain-helper.service"
      ];
      wants = [ "network-online.target" ];
      requires = [ "tokendrain-helper.service" ];
      environment = {
        TOKENDRAIN_STATE_DIR = cfg.stateDirectory;
        TOKENDRAIN_HELPER_SOCKET = "/run/tokendrain/helper.sock";
        TOKENDRAIN_GUEST_ARTIFACTS = toString cfg.microvm.guestArtifacts;
        TOKENDRAIN_MASTER_KEY_FILE = "/run/tokendrain-auth/master-key";
        TOKENDRAIN_LISTEN_ADDRESS = cfg.web.listenAddress;
        TOKENDRAIN_PUBLIC_URL = cfg.web.publicUrl;
        TOKENDRAIN_AUTH_MODE = cfg.auth.mode;
        TOKENDRAIN_ADMIN_TOKEN_FILE = "/run/tokendrain-auth/admin-token";
        TOKENDRAIN_AUTH_RUNTIME_DIR = "/run/tokendrain-auth";
        TOKENDRAIN_PORT = toString cfg.web.port;
        TOKENDRAIN_MAX_CONCURRENCY = toString cfg.concurrency;
        TOKENDRAIN_CONCURRENCY_LIMIT = toString cfg.concurrency;
        TOKENDRAIN_VCPUS_LIMIT = toString cfg.microvm.maxVcpus;
        TOKENDRAIN_MEMORY_MIB_LIMIT = toString cfg.microvm.maxMemoryMiB;
        TOKENDRAIN_DEFAULT_VCPUS = toString cfg.microvm.defaults.vcpus;
        TOKENDRAIN_DEFAULT_MEMORY_MIB = toString cfg.microvm.defaults.memoryMiB;
        TOKENDRAIN_DEFAULT_DISK_GIB = toString cfg.microvm.defaults.diskGiB;
        TOKENDRAIN_WEB_DIR = "${cfg.package}/share/tokendrain/web";
      };
      path = [
        cfg.codexPackage
        pkgs.git
        pkgs.coreutils
        pkgs.e2fsprogs
      ];
      serviceConfig = {
        # systemd may expose credentials with group-readable mode under its
        # protected mount. Copy to a private tmpfs file for the app's 0600 rule.
        ExecStartPre = [
          "${pkgs.coreutils}/bin/install -m 0600 %d/master-key /run/tokendrain-auth/master-key"
        ]
        ++ lib.optional (
          cfg.auth.mode == "token"
        ) "${pkgs.coreutils}/bin/install -m 0600 %d/admin-token /run/tokendrain-auth/admin-token";
        ExecStart = "${cfg.package}/bin/tokendraind";
        User = "tokendrain";
        Group = "tokendrain";
        RuntimeDirectory = "tokendrain-auth";
        RuntimeDirectoryMode = "0700";
        LoadCredential = [
          "master-key:${
            if cfg.masterKeyFile == null then "/var/lib/tokendrain-keys/master.key" else cfg.masterKeyFile
          }"
        ]
        ++ lib.optional (
          cfg.auth.mode == "token" && cfg.auth.adminTokenFile != null
        ) "admin-token:${cfg.auth.adminTokenFile}";
        WorkingDirectory = cfg.stateDirectory;
        Restart = "on-failure";
        RestartSec = 3;
        UMask = "0077";
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
        ];
        ReadWritePaths = [
          cfg.stateDirectory
          "/run/tokendrain-auth"
        ];
        TimeoutStopSec = 120;
      };
    };
  };
}
