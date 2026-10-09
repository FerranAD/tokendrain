{ pkgs, tokendrainModule }:
pkgs.testers.runNixOSTest {
  name = "tokendrain-vpn-forwarding";
  nodes.machine = { lib, ... }: {
    imports = [ tokendrainModule ];
    documentation.enable = false;
    services.tokendrain = {
      enable = true;
      auth.mode = "none";
      package = pkgs.emptyDirectory;
      codexPackage = pkgs.emptyDirectory;
      claudeCodePackage = pkgs.emptyDirectory;
      microvm.guestArtifacts = pkgs.emptyDirectory;
    };
    # Test the installed firewall independently of VMs and account credentials.
    systemd.services.tokendraind.enable = lib.mkForce false;
    systemd.services.tokendrain-helper.enable = lib.mkForce false;
    services.tailscale.enable = true;
    # Simulate tailscale0 with a veth, without an external tailnet or login.
    systemd.services.tailscaled.enable = lib.mkForce false;
    networking.firewall.allowedTCPPorts = [ 8080 ];
    environment.systemPackages = [
      pkgs.curl
      pkgs.python312
    ];
    virtualisation.memorySize = 1024;
  };
  testScript = ''
    machine.start()
    machine.wait_for_unit("nftables.service")
    rules = machine.succeed("nft -j list ruleset")
    import json
    assert not any(item.get("chain", {}).get("table") == "nixos-fw"
                   and item.get("chain", {}).get("hook") == "forward"
                   for item in json.loads(rules)["nftables"]), rules

    machine.succeed("ip netns add peer")
    machine.succeed("ip netns add endpoint")
    machine.succeed("ip link add tailscale0 type veth peer name peer0 netns peer")
    machine.succeed("ip link add uplink type veth peer name peer0 netns endpoint")
    machine.succeed("ip address add 100.127.0.1/30 dev tailscale0")
    machine.succeed("ip address add 203.0.113.1/30 dev uplink")
    machine.succeed("ip link set tailscale0 up")
    machine.succeed("ip link set uplink up")
    machine.succeed("ip -n peer address add 100.127.0.2/30 dev peer0")
    machine.succeed("ip -n peer link set peer0 up")
    machine.succeed("ip -n peer route add default via 100.127.0.1")
    machine.succeed("ip -n endpoint address add 203.0.113.2/30 dev peer0")
    machine.succeed("ip -n endpoint link set peer0 up")
    machine.succeed("ip -n endpoint route add default via 203.0.113.1")
    machine.succeed("ip netns exec endpoint python -u -m http.server 8080 >/tmp/endpoint-http.log 2>&1 </dev/null &")
    machine.succeed("python -u -m http.server 8080 >/tmp/host-http.log 2>&1 </dev/null &")
    host_request = "curl --fail --connect-timeout 2 http://203.0.113.2:8080/"
    peer_request = "ip netns exec peer " + host_request
    machine.wait_until_succeeds(peer_request)
    # Tailscale addresses overlapping the VM pool must not be masqueraded.
    machine.succeed("grep '100.127.0.2' /tmp/endpoint-http.log")
    machine.succeed(host_request)

    # Identical traffic on a guest interface must still face guest isolation.
    machine.succeed("ip link set tailscale0 name tdt-test")
    machine.succeed("nft add element inet tokendrain guest_sources '{ \"tdt-test\" . 100.127.0.2 }'")
    machine.succeed("nft flush chain inet tokendrain routing_ready")
    machine.succeed("nft add rule inet tokendrain routing_ready return")
    machine.fail(peer_request)
    machine.fail("ip netns exec peer curl --fail --connect-timeout 2 http://100.127.0.1:8080/")
    machine.succeed(host_request)
    machine.succeed("ip link set tdt-test name tailscale0")
    machine.succeed(peer_request)
  '';
}
