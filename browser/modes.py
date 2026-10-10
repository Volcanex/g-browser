"""Modes: which persona the browser is wearing.

A mode bundles the three things that together constitute an identity: the exit
IP, the cookie jar, and the fingerprint. They are bundled rather than set
individually because they only make sense together. The owner's cookies leaving
from a datacenter IP is worse than either choice alone.

- **identity**: the owner's real Chrome profile out through the sticky
  residential IP. Metered, careful, and the only mode allowed to act as them:
  logging in, filling applications, pressing submit.
- **research**: a separate profile that has never seen their accounts, out
  through a cheap datacenter IP. For reading public job boards and anything
  else where being obviously automated costs nothing. Bandwidth here is
  effectively free next to residential's metered plan, which is the whole
  reason the split exists.
- **direct**: no proxy, research profile. Development and local testing only;
  it exits from the bare Hetzner IP, which is fine for public pages and wrong
  for everything else.

**A job must name its mode.** There is deliberately no default: defaulting would
mean a consumer that forgot to think about it silently borrows the owner's
identity, and that is the one failure this design exists to prevent.

Honest limit on the separation: both modes run the same Chrome on the same Xvfb
display, so canvas, WebGL and font fingerprints are identical between them. The
viewport differs, but a determined cross-site fingerprinter could still link the
two personas. The real separators are the IP and the cookie jar; treat the
fingerprint variation as friction, not a wall.

Because Patchright fixes both the proxy and the profile at
`launch_persistent_context` time, switching mode means relaunching Chrome. That
is why modes are coarse and why consumers should batch work by mode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import proxy

VAR = os.environ.get("GBROWSER_VAR") or os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "var")


@dataclass(frozen=True)
class Mode:
    name: str
    profile: str
    proxy_kind: str | None
    identity: bool           # may this mode act as the owner?
    viewport: dict
    locale: str
    timezone: str
    description: str

    @property
    def label(self) -> str:
        return f"{self.name} ({proxy.proxy_label(self.proxy_kind)})"


IDENTITY = Mode(
    name="identity",
    profile=os.environ.get("GBROWSER_PROFILE", os.path.join(VAR, "profile")),
    proxy_kind=proxy.STATIC,
    identity=True,
    viewport={"width": 1920, "height": 1080},
    locale="en-GB",
    timezone="Europe/London",
    description="The owner's real profile via the sticky residential IP, metered, acts as the owner",
)

RESEARCH = Mode(
    name="research",
    profile=os.environ.get("GBROWSER_RESEARCH_PROFILE", os.path.join(VAR, "profile-research")),
    proxy_kind=proxy.DATACENTER,
    identity=False,
    # A different viewport is the one cheap fingerprint lever available without
    # lying about the browser itself. 1536x864 is the commonest laptop size.
    viewport={"width": 1536, "height": 864},
    locale="en-GB",
    timezone="Europe/London",
    description="Throwaway profile via a datacenter IP, public reading, no identity attached",
)

DIRECT = Mode(
    name="direct",
    profile=RESEARCH.profile,
    proxy_kind=None,
    identity=False,
    viewport={"width": 1536, "height": 864},
    locale="en-GB",
    timezone="Europe/London",
    description="No proxy, bare host IP, research profile, development only",
)

MODES = {m.name: m for m in (IDENTITY, RESEARCH, DIRECT)}

DEFAULT_MODE = os.environ.get("GBROWSER_DEFAULT_MODE")  # unset on purpose


def resolve(name: str | None) -> Mode:
    """Mode by name. Fails closed: an unnamed mode is an error, never identity."""
    if not name:
        name = DEFAULT_MODE
    if not name:
        raise ValueError(
            "job has no `mode`. Name one of: "
            + ", ".join(f"{k} ({v.description})" for k, v in MODES.items())
        )
    mode = MODES.get(name)
    if mode is None:
        raise ValueError(f"unknown mode {name!r}, expected one of {', '.join(MODES)}")
    return mode


def describe() -> list[dict]:
    return [
        {
            "name": m.name,
            "profile": m.profile,
            "egress": proxy.proxy_label(m.proxy_kind),
            "identity": m.identity,
            "description": m.description,
        }
        for m in MODES.values()
    ]
