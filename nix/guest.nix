{
  config,
  lib,
  pkgs,
  tokendrainPackage,
  ...
}:
{
  system.stateVersion = "26.05";
  networking.hostName = "tokendrain-project";
  microvm = {
    hypervisor = "firecracker";
    vcpu = 4;
    mem = 4096;
    socket = "firecracker.sock";
    vsock.cid = 3;
    writableStoreOverlay = "/persist";
    volumes = [
      {
        image = "environment.img";
        mountPoint = "/persist";
        size = 40960;
        autoCreate = false;
        fsType = "ext4";
      }
      {
        image = "workspace.img";
        mountPoint = "/workspace";
        size = 40960;
        autoCreate = false;
        fsType = "ext4";
      }
    ];
  };
  fileSystems."/persist".neededForBoot = true;
  fileSystems."/workspace".neededForBoot = true;
  boot.kernelModules = [
    "virtio_net"
    "vmw_vsock_virtio_transport"
  ];
  boot.kernelParams = [ "net.ifnames=0" ];
  boot.postBootCommands = lib.mkBefore ''
    # Run before microvm.nix registers the immutable closure in the Nix DB.
    mkdir -p /persist/root /persist/nix-var /persist/var-lib /persist/var-cache
    mkdir -p /root /nix/var /var/lib /var/cache
    mount --bind /persist/root /root
    mount --bind /persist/nix-var /nix/var
    mount --bind /persist/var-lib /var/lib
    mount --bind /persist/var-cache /var/cache
    if [[ " $(cat /proc/cmdline)" =~ [[:space:]]ip=([^[:space:]]+) ]]; then
      IFS=: read -r address _ gateway _ _ _ _ <<< "''${BASH_REMATCH[1]}"
      mkdir -p /run/systemd/network
      cat > /run/systemd/network/10-tokendrain.network <<NETWORK
    [Match]
    Name=eth0
    [Network]
    Address=$address/30
    Gateway=$gateway
    DNS=1.1.1.1
    DNS=9.9.9.9
    IPv6AcceptRA=no
    LinkLocalAddressing=no
    NETWORK
    fi
  '';
  networking = {
    useDHCP = false;
    useNetworkd = true;
    enableIPv6 = false;
    nameservers = [
      "1.1.1.1"
      "9.9.9.9"
    ];
    firewall.enable = false;
  };
  services.resolved.enable = false;
  services.openssh.enable = false;
  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    auto-optimise-store = false;
    trusted-users = [ "root" ];
  };
  nix.gc.automatic = false;
  environment.systemPackages = with pkgs; [
    git
    curl
    jq
    gcc
    gnumake
    cmake
    pkg-config
    python312
    nodejs_22
    rustup
    cacert
    nix
    codex
    tokendrainPackage
    util-linux
    iproute2
    ripgrep
    fd
    unzip
    gnutar
    gzip
    xz
    file
    procps
    gh
  ];
  environment.variables = {
    HOME = "/root";
    CODEX_HOME = "/persist/codex";
    SSL_CERT_FILE = "/etc/ssl/certs/ca-bundle.crt";
  };
  users.users.root.initialHashedPassword = "!";
  systemd.services.tokendrain-guestd = {
    description = "Tokendrain guest supervisor (vsock)";
    wantedBy = [ "multi-user.target" ];
    after = [
      "local-fs.target"
      "network.target"
    ];
    requires = [
      "persist.mount"
      "workspace.mount"
    ];
    path = config.environment.systemPackages;
    environment = config.environment.variables;
    serviceConfig = {
      ExecStart = "${tokendrainPackage}/bin/tokendrain-guestd --poweroff-on-shutdown";
      User = "root";
      Restart = "on-failure";
      RestartSec = 2;
      RuntimeDirectory = "tokendrain";
      RuntimeDirectoryMode = "0700";
      UMask = "0077";
      KillMode = "control-group";
      TimeoutStopSec = 30;
      WorkingDirectory = "/workspace";
    };
  };
}
