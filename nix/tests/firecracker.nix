{
  pkgs,
  tokendrainModule,
  guestArtifacts,
}:
pkgs.testers.runNixOSTest {
  name = "tokendrain-firecracker";
  nodes = {
    machine = { lib, ... }: {
      imports = [ tokendrainModule ];
      documentation.enable = false;
      services.tokendrain = {
        enable = true;
        microvm.guestArtifacts = guestArtifacts;
        microvm.defaults.memoryMiB = 1536;
        microvm.defaults.diskGiB = 1;
      };
      # Exercise helper independently of OpenAI credentials and the API daemon.
      systemd.services.tokendraind.enable = lib.mkForce false;
      virtualisation = {
        memorySize = 8192;
        diskSize = 8192;
        cores = 4;
        qemu.options = [ "-cpu host" ];
      };
      boot.kernelModules = lib.optionals pkgs.stdenv.hostPlatform.isx86_64 [
        "kvm-intel"
        "kvm-amd"
      ];
      networking.interfaces.eth1.ipv4.addresses = lib.mkForce [
        {
          address = "192.168.1.1";
          prefixLength = 24;
        }
      ];
      networking.interfaces.eth1.ipv4.routes = [
        {
          address = "8.8.8.8";
          prefixLength = 32;
          via = "192.168.1.2";
        }
      ];
      environment.systemPackages = [
        pkgs.python312
        pkgs.curl
      ];
      systemd.services.test-host-http = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig.ExecStart = "${pkgs.python312}/bin/python -m http.server 8080";
      };
    };
    internet = { lib, ... }: {
      documentation.enable = false;
      networking.interfaces.eth1.ipv4.addresses = lib.mkForce [
        {
          address = "192.168.1.2";
          prefixLength = 24;
        }
      ];
      networking.firewall.allowedTCPPorts = [ 8080 ];
      systemd.services.test-public-http = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig.ExecStart = "${pkgs.python312}/bin/python -m http.server 8080";
      };
    };
  };
  testScript = ''
    start_all()
    machine.wait_for_unit("tokendrain-helper.service")
    internet.wait_for_unit("test-public-http.service")
    machine.succeed("test -c /dev/kvm")
    internet.wait_for_open_port(8080)
    machine.wait_for_open_port(8080)
    machine.succeed("ip address replace 9.9.9.9/32 dev lo")
    internet.succeed("ip address replace 8.8.8.8/32 dev lo")
    machine.succeed("ip address replace 93.184.215.1/24 dev eth1")
    internet.succeed("ip address replace 93.184.215.2/24 dev eth1")
    machine.succeed("curl --fail --connect-timeout 2 http://93.184.215.2:8080/")
    machine.wait_until_succeeds("curl --fail --connect-timeout 2 http://8.8.8.8:8080/", timeout=20)
    try:
        machine.succeed("${pkgs.python312}/bin/python ${./helper-smoke.py}", timeout=240)
    except Exception:
        machine.log(machine.succeed("journalctl -u 'tokendrain*' --no-pager -n 400"))
        raise
  '';
}
