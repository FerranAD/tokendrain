{ lib, pkgs, ... }:
{
  documentation.enable = false;
  system.stateVersion = "26.05";
  networking.hostName = "tokendrain-project";
  boot.loader.grub.enable = false;
  boot.initrd.systemd.enable = false;
  boot.initrd.availableKernelModules = [
    "virtio_pci"
    "virtio_mmio"
    "virtio_blk"
  ];
  # Load host-kernel drivers before entering the project's long-lived userspace.
  # Its historical module tree may belong to a different kernel after a host upgrade.
  boot.initrd.kernelModules = [
    "virtio_net"
    "vmw_vsock_virtio_transport"
  ];
  boot.kernelParams = [ "net.ifnames=0" ];
  fileSystems."/" = {
    device = "/dev/vda";
    fsType = "ext4";
  };
  fileSystems."/run/tokendrain-control" = {
    device = "/dev/vdb";
    fsType = "ext4";
    options = [
      "ro"
      "nodev"
      "nosuid"
    ];
  };
  boot.postBootCommands = ''
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
    SSL_CERT_FILE = "/etc/ssl/certs/ca-bundle.crt";
  };
  users.users.root.initialHashedPassword = "!";
  systemd.tmpfiles.rules = [ "d /workspace 0755 root root -" ];
  # Stable boot contract. All version-specific control code is on the attached volume.
  systemd.services.tokendrain-control = {
    description = "Activate current Tokendrain control bundle";
    wantedBy = [ "multi-user.target" ];
    after = [
      "local-fs.target"
      "network.target"
    ];
    requires = [ "run-tokendrain\\x2dcontrol.mount" ];
    path = [
      pkgs.nix
      pkgs.coreutils
    ];
    environment = {
      HOME = "/root";
    };
    serviceConfig = {
      ExecStart = "/run/tokendrain-control/activate";
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
