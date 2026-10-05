{
  pkgs,
  guestd,
  codex ? pkgs.codex,
}:
let
  manifest = pkgs.writeText "tokendrain-control-manifest.json" (
    builtins.toJSON {
      version = 1;
      guestd = toString guestd;
      codex = toString codex;
    }
  );
  activate = pkgs.writeText "tokendrain-control-activate" ''
    #!/bin/sh
    set -eu
    control=/run/tokendrain-control
    # Import missing paths into the real persistent store and register their references.
    # This is a trusted, host-built local cache, not an Internet substituter.
    nix --extra-experimental-features nix-command copy --no-check-sigs \
      --from "file://$control/cache" ${guestd} ${codex}
    mkdir -p /nix/var/nix/gcroots/tokendrain-control
    ln -sfn ${guestd} /nix/var/nix/gcroots/tokendrain-control/guestd
    ln -sfn ${codex} /nix/var/nix/gcroots/tokendrain-control/codex
    export PATH=/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin:$PATH
    export TOKENDRAIN_CONTROL_GUESTD=${guestd}
    exec ${guestd}/bin/tokendrain-guestd --poweroff-on-shutdown \
      --codex ${codex}/bin/codex --codex-home /root/.local/share/tokendrain/codex
  '';
in
pkgs.runCommand "tokendrain-control"
  {
    nativeBuildInputs = [
      pkgs.nix
      pkgs.e2fsprogs
    ];
  }
  ''
    mkdir -p payload/cache $out
    export NIX_STATE_DIR=$TMPDIR/nix-state
    nix-store --load-db < ${
      pkgs.closureInfo {
        rootPaths = [
          guestd
          codex
        ];
      }
    }/registration
    nix --extra-experimental-features nix-command copy \
      --to "file://$PWD/payload/cache?compression=none" ${guestd} ${codex}
    cp ${manifest} payload/manifest.json
    cp ${activate} payload/activate
    chmod 0555 payload/activate
    size=$(du -sm payload | cut -f1)
    truncate -s "$((size + size / 8 + 64))M" $out/control.img
    mkfs.ext4 -q -F -L td-control -d payload $out/control.img
  ''
