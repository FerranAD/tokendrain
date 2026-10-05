{
  pkgs,
  guest,
  control,
}:
let
  cfg = guest.config;
  base = import (pkgs.path + "/nixos/lib/make-disk-image.nix") {
    inherit pkgs;
    inherit (pkgs) lib;
    config = cfg;
    name = "tokendrain-base-vm";
    partitionTableType = "none";
    installBootLoader = false;
    copyChannel = false;
    format = "raw";
    label = "td-vm";
  };
  kernel =
    if pkgs.stdenv.hostPlatform.isx86_64 then
      "${cfg.system.build.kernel.dev}/vmlinux"
    else
      "${cfg.system.build.kernel}/${cfg.system.boot.loader.kernelFile}";
  manifest = pkgs.writeText "tokendrain-guest-manifest.json" (
    builtins.toJSON {
      version = 2;
      # Boot the OS profile belonging to this project, never the host's current userspace.
      boot_args = "console=ttyS0,115200 reboot=k panic=1 root=/dev/vda rw init=/nix/var/nix/profiles/system/init ${toString cfg.boot.kernelParams}";
      inherit (cfg.system) stateVersion;
    }
  );
in
pkgs.runCommand "tokendrain-guest-artifacts" { } ''
  mkdir -p $out
  ln -s ${kernel} $out/kernel
  ln -s ${cfg.system.build.initialRamdisk}/initrd $out/initrd
  ln -s ${base}/nixos.img $out/base.img
  ln -s ${control}/control.img $out/control.img
  ln -s ${manifest} $out/manifest.json
''
