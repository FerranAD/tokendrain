pkgs:
pkgs.firecracker.overrideAttrs (old: {
  # Drop once the nixpkgs pin includes the Firecracker fix for NixOS/nixpkgs#570938.
  postConfigure = (old.postConfigure or "") + ''
    unset AWS_LC_SYS_USE_SYSTEM
  '';
})
