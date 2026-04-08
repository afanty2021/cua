"""Topology — multi-container/multi-VM sandbox wiring.

A Topology pairs a primary Image with optional sidecars:

* **proxy** — a transparent network interceptor (iptables + CA auto-configured)
* **services** — named peers the primary can reach by hostname

Usage::

    from cua_sandbox import Image, Sandbox
    from cua_sandbox.mitm import MitmProxy

    mitm   = MitmProxy.replace("example.com", "old text", "new text")
    world  = Image.base("python:3.12-slim").run("pip install androidworld-server")

    topology = (
        Image.android("14")
        .with_proxy(mitm)
        .with_service("world", world)
    )

    async with Sandbox.ephemeral(topology, local=True) as sb:
        await sb.mouse.click(100, 200)
        flows = await sb.proxy.flows()
        await sb.services["world"].shell.run("androidworld-cli reset")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from cua_sandbox.image import Image
    from cua_sandbox.interfaces.shell import Shell


@dataclass
class ServiceHandle:
    """Runtime handle for a named service sidecar (``sb.services["name"]``)."""

    name: str
    shell: "Shell"
    host: str  # hostname reachable from the primary container
    api_url: Optional[str] = None  # computer-server URL if the sidecar has one


@dataclass
class Topology:
    """Declarative wiring between a primary Image and its sidecars.

    Build via ``Image.with_proxy()`` / ``Image.with_service()`` — do not
    construct directly unless you need full control.

    Attributes:
        primary:  The main sandbox image (has the display, mouse, keyboard, …).
        proxy:    Optional transparent-proxy sidecar image.  When set,
                  the runtime automatically configures iptables DNAT and installs
                  the proxy CA into the primary container's system trust store.
        services: Named peer images.  Each becomes reachable from the primary
                  under the key as a hostname on the shared Docker network.
    """

    primary: "Image"
    proxy: Optional["Image"] = None
    services: Dict[str, "Image"] = field(default_factory=dict)

    def with_proxy(self, proxy_image: "Image") -> "Topology":
        """Attach (or replace) a transparent proxy sidecar."""
        return Topology(primary=self.primary, proxy=proxy_image, services=dict(self.services))

    def with_service(self, name: str, service_image: "Image") -> "Topology":
        """Add a named service sidecar."""
        return Topology(
            primary=self.primary,
            proxy=self.proxy,
            services={**self.services, name: service_image},
        )
