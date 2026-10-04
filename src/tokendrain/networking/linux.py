from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import AsyncIterator, Sequence
from uuid import UUID

from pydantic import BaseModel

from tokendrain.vm.commands import CommandResult, Runner

# Includes RFC1918, link-local/metadata, carrier-grade NAT, documentation,
# multicast and reserved ranges. IPv6 is disabled/blocked for VM interfaces.
BLOCKED_V4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


class NetworkAllocation(BaseModel):
    execution_id: str
    tap: str
    host_ip: str
    guest_ip: str
    prefix: int = 30
    mac: str


def allocation(execution_id: str, slot: int) -> NetworkAllocation:
    value = UUID(execution_id)
    if not 0 <= slot < 16384:
        raise ValueError("Network slot out of range")
    network = ipaddress.IPv4Network((int(ipaddress.IPv4Address("100.127.0.0")) + slot * 4, 30))
    return NetworkAllocation(
        execution_id=str(value),
        tap=f"tdt{value.hex[:11]}",
        host_ip=str(network.network_address + 1),
        guest_ip=str(network.network_address + 2),
        mac="02:54:" + ":".join(f"{b:02x}" for b in value.bytes[-4:]),
    )


def firewall_rules(allow_lan: bool = False) -> str:
    blocked = (
        ""
        if allow_lan
        else "ip daddr @lan4 drop\nip daddr { " + ", ".join(BLOCKED_V4) + " } drop\n"
    )
    return f"""table inet tokendrain {{
 set lan4 {{ type ipv4_addr; flags interval; auto-merge; }}
 set guest_sources {{ type ifname . ipv4_addr; }}
 chain source_guard {{
  type filter hook prerouting priority -310; policy accept;
  iifname "tdt*" iifname . ip saddr @guest_sources return
  iifname "tdt*" drop
 }}
 chain routing_ready {{ drop; }}
 chain host_guard {{
  type filter hook input priority -20; policy accept;
  iifname "tdt*" drop
 }}
 chain guests {{
  type filter hook forward priority -20; policy accept;
  iifname "tdt*" oifname "tdt*" drop
  iifname "tdt*" meta nfproto ipv6 drop
  oifname "tdt*" meta nfproto ipv6 drop
  iifname "tdt*" fib daddr type local drop
  iifname "tdt*" jump egress
  oifname "tdt*" ct state established,related accept
  oifname "tdt*" drop
 }}
 chain egress {{
  jump routing_ready
  {blocked}
 }}
 chain nat {{
  type nat hook postrouting priority srcnat; policy accept;
  ip saddr 100.127.0.0/16 oifname != "tdt*" masquerade
 }}
}}
"""


class LinuxNetwork:
    """TAP interfaces are isolated by an early nftables input/forward policy.

    No bridge is used. Each VM has a unique /30 and can only route through the
    host. The NixOS module installs the global firewall before this helper starts.
    Per-VM source filtering prevents spoofing across guests.
    """

    def __init__(self, runner: Runner, vm_uid: int) -> None:
        self.runner = runner
        self.vm_uid = vm_uid
        self._policy_lock = asyncio.Lock()

    async def refresh_policy(self, guests: Sequence[NetworkAllocation]) -> None:
        async with self._policy_lock:
            await self._refresh_policy(guests)

    async def _refresh_policy(self, guests: Sequence[NetworkAllocation]) -> None:
        result = await self.runner.run("ip", "-json", "-4", "route", "show", "table", "all")
        routes = json.loads(result.stdout)
        networks = set()
        for route in routes:
            if (
                route.get("scope") == "link"
                and route.get("dst") != "default"
                and not route.get("dev", "").startswith("tdt")
            ):
                networks.add(str(ipaddress.IPv4Network(route["dst"], strict=False)))
        rules = "flush set inet tokendrain lan4\n"
        if networks:
            rules += "add element inet tokendrain lan4 { " + ", ".join(sorted(networks)) + " }\n"
        rules += "flush set inet tokendrain guest_sources\n"
        if guests:
            elements = ", ".join(f'"{guest.tap}" . {guest.guest_ip}' for guest in guests)
            rules += "add element inet tokendrain guest_sources { " + elements + " }\n"
        rules += (
            "flush chain inet tokendrain routing_ready\n"
            "add rule inet tokendrain routing_ready return\n"
        )
        # One transaction: default-drop egress only opens with all guards ready.
        await self.runner.run("nft", "-f", "-", input=rules.encode())

    async def deny_egress(self) -> None:
        await self.runner.run(
            "nft",
            "-f",
            "-",
            input=(
                b"flush chain inet tokendrain routing_ready\n"
                b"add rule inet tokendrain routing_ready drop\n"
            ),
        )

    async def route_changes(self) -> AsyncIterator[None]:
        # Linux rtnetlink multicast groups: LINK, IPV4_IFADDR, IPV4_ROUTE.
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as channel:
            channel.bind((0, 1 | 0x10 | 0x40))
            channel.setblocking(False)
            while True:
                await asyncio.get_running_loop().sock_recv(channel, 65536)
                yield None

    async def create(self, network: NetworkAllocation) -> None:
        await self.runner.run(
            "ip", "tuntap", "add", "dev", network.tap, "mode", "tap", "user", str(self.vm_uid)
        )
        try:
            await self.runner.run(
                "ip", "address", "add", f"{network.host_ip}/30", "dev", network.tap
            )
            rule = (
                f"add element inet tokendrain guest_sources "
                f'{{ "{network.tap}" . {network.guest_ip} }}\n'
            )
            await self.runner.run("nft", "-f", "-", input=rule.encode())
            await self.runner.run("ip", "link", "set", network.tap, "up")
        except BaseException:
            await self.remove(network)
            raise

    @staticmethod
    def _removed(result: CommandResult) -> None:
        if result.returncode == 0:
            return
        error = result.stderr.decode(errors="replace").lower()
        if any(
            marker in error
            for marker in ("does not exist", "cannot find device", "no such file or directory")
        ):
            return
        raise RuntimeError(f"Network cleanup failed: {error[-2048:]}")

    async def remove(self, network: NetworkAllocation) -> None:
        result = await self.runner.run("ip", "link", "delete", network.tap, check=False)
        self._removed(result)
        rule = (
            f"delete element inet tokendrain guest_sources "
            f'{{ "{network.tap}" . {network.guest_ip} }}\n'
        )
        result = await self.runner.run("nft", "-f", "-", input=rule.encode(), check=False)
        self._removed(result)
