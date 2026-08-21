"""Shape a Docker container's egress with tc/netem from the host.

The shell scenarios do this inline with `docker exec <client> tc ...`;
this is the same thing as a small Python helper for ad-hoc experiments or
for writing a scenario in Python. It runs `tc` *inside* the target
container (which needs the NET_ADMIN capability — docker-compose.yml
grants it to the client), so the host's network is never touched.

    from network_conditions import NetworkConditionManager
    net = NetworkConditionManager("sync-client")
    net.apply(delay="100ms", loss="30%")     # degraded link
    net.cut()                                # total outage
    net.restore()                            # back to normal

Or from a shell:

    python utils/network_conditions.py sync-client apply --delay 100ms --loss 30%
    python utils/network_conditions.py sync-client cut
    python utils/network_conditions.py sync-client restore
"""

from __future__ import annotations

import argparse
import subprocess


class NetworkConditionManager:
    """Apply, replace and clear netem/tbf qdiscs on one container interface."""

    def __init__(self, container: str, interface: str = "eth0"):
        self.container = container
        self.interface = interface

    def _tc(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "exec", self.container, "tc", *args],
            check=check, capture_output=True, text=True)

    def apply(self, delay: str = "0ms", loss: str = "0%", rate: str | None = None,
              burst: str = "32kbit", latency: str = "400ms") -> None:
        """Replace whatever is configured with the given conditions.

        `delay` and `loss` go to a root netem qdisc; `rate` (e.g. "1mbit"),
        if given, adds a token-bucket filter beneath it to cap bandwidth.
        """
        self.clear()
        netem = ["qdisc", "add", "dev", self.interface, "root", "handle", "1:", "netem"]
        if delay != "0ms":
            netem += ["delay", delay]
        if loss != "0%":
            netem += ["loss", loss]
        self._tc(*netem)
        if rate:
            self._tc("qdisc", "add", "dev", self.interface, "parent", "1:1",
                     "handle", "10:", "tbf", "rate", rate, "burst", burst,
                     "latency", latency)

    def clear(self) -> None:
        """Remove all shaping; a no-op if none is configured."""
        self._tc("qdisc", "del", "dev", self.interface, "root", check=False)

    def cut(self) -> None:
        """Simulate a total outage: every packet dropped."""
        self.apply(loss="100%")

    def restore(self) -> None:
        """Back to the unshaped link."""
        self.clear()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point; see the module docstring for examples."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("container", help="container name, e.g. sync-client")
    p.add_argument("--interface", default="eth0")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply", help="set delay/loss/rate")
    a.add_argument("--delay", default="0ms")
    a.add_argument("--loss", default="0%")
    a.add_argument("--rate", default=None)
    sub.add_parser("cut", help="100%% loss (total outage)")
    sub.add_parser("restore", help="clear all shaping")
    args = p.parse_args(argv)
    net = NetworkConditionManager(args.container, args.interface)
    if args.cmd == "apply":
        net.apply(delay=args.delay, loss=args.loss, rate=args.rate)
    elif args.cmd == "cut":
        net.cut()
    else:
        net.restore()


if __name__ == "__main__":
    main()
