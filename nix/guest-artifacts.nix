{ pkgs, guest }:
let
  cfg = guest.config;
  kernel =
    if pkgs.stdenv.hostPlatform.isx86_64 then
      "${cfg.microvm.kernel.dev}/vmlinux"
    else
      "${cfg.microvm.kernel}/${cfg.system.boot.loader.kernelFile}";
  manifest = pkgs.writeText "tokendrain-guest-manifest.json" (
    builtins.toJSON {
      version = 1;
      boot_args = "console=ttyS0,115200 reboot=k panic=1 i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd ${toString cfg.microvm.kernelParams}";
      inherit (cfg.system) stateVersion;
    }
  );
in
pkgs.runCommand "tokendrain-guest-artifacts" { } ''
  mkdir -p $out
  ln -s ${kernel} $out/kernel
  ln -s ${cfg.microvm.initrdPath} $out/initrd
  ln -s ${cfg.microvm.storeDisk} $out/store.img
  ln -s ${manifest} $out/manifest.json
''
