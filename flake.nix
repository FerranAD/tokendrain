{
  description = "Persistent projects and disposable autonomous Codex microVMs";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    microvm.url = "github:microvm-nix/microvm.nix";
    microvm.inputs.nixpkgs.follows = "nixpkgs";
  };
  outputs =
    inputs@{
      self,
      nixpkgs,
      microvm,
      ...
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      eachSystem = nixpkgs.lib.genAttrs systems;
      mkGuest =
        system:
        nixpkgs.lib.nixosSystem {
          inherit system;
          specialArgs.tokendrainPackage = self.packages.${system}.tokendrain-guestd;
          modules = [
            microvm.nixosModules.microvm
            ./nix/guest.nix
          ];
        };
    in
    {
      packages = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          app = pkgs.callPackage ./nix/package.nix { };
          web = pkgs.callPackage ./nix/web.nix { };
          guest = mkGuest system;
        in
        {
          tokendrain = app.overrideAttrs (old: {
            postInstall = (old.postInstall or "") + ''
              mkdir -p $out/share/tokendrain
              ln -s ${web} $out/share/tokendrain/web
            '';
          });
          tokendrain-guestd = app;
          guest-artifacts = pkgs.callPackage ./nix/guest-artifacts.nix { inherit guest; };
          inherit web;
          default = self.packages.${system}.tokendrain;
        }
      );
      nixosModules.tokendrain = { pkgs, lib, ... }: {
        imports = [ ./modules/tokendrain.nix ];
        services.tokendrain.codexPackage =
          lib.mkDefault
            nixpkgs.legacyPackages.${pkgs.stdenv.hostPlatform.system}.codex;
        services.tokendrain.package =
          lib.mkDefault
            self.packages.${pkgs.stdenv.hostPlatform.system}.tokendrain;
        services.tokendrain.microvm.guestArtifacts =
          lib.mkDefault
            self.packages.${pkgs.stdenv.hostPlatform.system}.guest-artifacts;
      };
      devShells = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python312
              uv
              ruff
              mypy
              nodejs_22
              git
              firecracker
              e2fsprogs
              iproute2
              nftables
              curl
              jq
              nixfmt
            ];
            LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
            shellHook = ''
              export UV_PYTHON=${pkgs.python312}/bin/python3
            '';
          };
        }
      );
      checks = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          package = self.packages.${system}.tokendrain;
          python = self.packages.${system}.tokendrain-guestd.override { doCheck = true; };
          firecracker = import ./nix/tests/firecracker.nix {
            inherit pkgs;
            tokendrainModule = self.nixosModules.tokendrain;
            guestArtifacts = self.packages.${system}.guest-artifacts;
          };
          module = pkgs.testers.runNixOSTest {
            name = "tokendrain-service";
            nodes.machine = { ... }: {
              imports = [ self.nixosModules.tokendrain ];
              documentation.enable = false;
              services.tokendrain.enable = true;
              services.tokendrain.auth.mode = "none";
              # Regression: coexist with forwarding already enabled by a VPN module.
              boot.kernel.sysctl."net.ipv4.ip_forward" = 1;
              # Service/API smoke test does not boot a nested Firecracker VM.
              services.tokendrain.microvm.guestArtifacts = pkgs.emptyDirectory;
              virtualisation.memorySize = 2048;
            };
            testScript = ''
              import json
              machine.start()
              machine.wait_for_unit("tokendraind.service")
              machine.wait_for_open_port(8742)
              assert machine.succeed("sysctl -n net.ipv4.ip_forward").strip() == "1"
              machine.succeed("curl --fail http://127.0.0.1:8742/healthz")
              assert json.loads(machine.succeed("tokendrain --json projects")) == []
              assert json.loads(machine.succeed("tokendrain --json runs")) == []
              status = json.loads(machine.succeed("tokendrain --json status"))
              checks = {check["name"]: check for check in status["checks"]}
              assert checks["helper_socket"]["ok"], checks["helper_socket"]
              assert checks["helper_socket"]["scope"] == "daemon", checks["helper_socket"]
              assert checks["helper"]["ok"], checks["helper"]
              assert checks["helper"]["scope"] == "helper", checks["helper"]
              for name in ("firecracker", "ip", "nft"):
                  assert checks[name]["ok"], checks[name]
                  assert checks[name]["scope"] == "helper", checks[name]
              for name in ("kvm", "tun", "guest_artifacts", "cgroup_v2", "ipv4_forwarding"):
                  assert checks[name]["scope"] == "helper", checks[name]
              # This smoke test deliberately omits usable guest artifacts.
              # Reporting through the helper must retain real failures.
              assert not checks["guest_artifacts"]["ok"], checks["guest_artifacts"]
              assert "nix" not in checks, checks
              for name in ("master_key", "database"):
                  assert checks[name]["ok"], checks[name]
                  assert checks[name]["scope"] == "daemon", checks[name]
              # Diagnostics must not weaken the web daemon's isolation to
              # make privileged devices and helper-only binaries visible.
              assert machine.succeed("systemctl show tokendraind -p PrivateDevices --value").strip() == "yes"
              assert machine.succeed("systemctl show tokendraind -p CapabilityBoundingSet --value").strip() == ""
              machine.succeed("nsenter -t $(systemctl show tokendraind -p MainPID --value) -m test ! -e /dev/kvm")
              assert machine.succeed("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8742/api/v1/projects") == "401"
              assert '<div id="root">' in machine.succeed("curl --fail http://127.0.0.1:8742/")
              machine.succeed("test $(stat -c %a /run/tokendrain-auth/master-key) = 600")
              machine.succeed("systemctl stop tokendraind")
              assert machine.succeed("systemctl show -p Result --value tokendraind").strip() == "success"
            '';
          };
        }
      );
      formatter = eachSystem (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
