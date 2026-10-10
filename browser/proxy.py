"""Egress selection: which IP the browser leaves from, per mode.

There are two Webshare products on one account and they are emphatically not
interchangeable:

- **residential** (`p.webshare.io`, sticky UK session encoded in the username).
  Metered and expensive. This is the egress that wears the owner's face, so it
  must hold ONE exit IP: `cf_clearance` and `datadome` cookies are IP-bound,
  and a cookie-rich profile that teleports self-flags.
- **datacenter** (a fixed pool of UK IPs, nearly free at our volume). Obviously
  a datacenter to anyone who checks, so it is for reading public job boards and
  never for anything carrying the identity.

Two gotchas found the hard way, worth not rediscovering:

- The `p.webshare.io` rotating endpoint serves the **residential** product only.
  Datacenter proxies must be addressed individually as `IP:PORT` from the proxy
  list; pointing datacenter credentials at `p.webshare.io` silently returns
  nothing at all.
- An exhausted residential plan answers `Bandwidth limit reached` in the body of
  a plain-HTTP response but *intermittently still tunnels HTTPS*. "Some requests
  work" is therefore not evidence of a healthy plan. Check it explicitly, which
  is what `residential_status()` is for.

Datacenter credentials are derived from `WEBSHARE_API_KEY` on first use rather
than stored as another secret in the passport, and cached in `var/` so a launch
never depends on the Webshare API being reachable.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from urllib.parse import quote

VAR = os.environ.get("GBROWSER_VAR") or os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "var")
_CACHE_TTL = 7 * 24 * 3600

RESIDENTIAL = "residential"      # rotating backbone (p.webshare.io), legacy
DATACENTER = "datacenter"        # shared datacenter pool
STATIC = "static"                # static residential ("ISP"), identity's egress

# Webshare files each product as a `proxy_subtype` on the plan. The plan id is
# discovered rather than hardcoded so a re-purchase doesn't need a code change.
_SUBTYPE = {DATACENTER: "default", STATIC: "isp"}

# Only override the sticky session segment when explicitly told to (e.g. to burn
# a flagged IP). The passport value is already a valid sticky UK-London session.
_STICKY_OVERRIDE = os.environ.get("GBROWSER_PROXY_SESSION")


def _disabled() -> bool:
    return os.environ.get("GBROWSER_PROXY_DISABLE", "").lower() in ("1", "true", "yes")


# ---- residential ----------------------------------------------------------

def _residential():
    host = os.environ.get("WEBSHARE_RESIDENTIAL_HOST")
    user = os.environ.get("WEBSHARE_RESIDENTIAL_USER")
    password = os.environ.get("WEBSHARE_RESIDENTIAL_PASS")
    if not (host and user and password):
        return None
    if _STICKY_OVERRIDE:
        parts = user.split("-")
        parts[-1] = _STICKY_OVERRIDE          # replace only the session segment
        user = "-".join(parts)
    server = host if ":" in host else f"{host}:80"
    return {"server": f"http://{server}", "username": user, "password": password}


# ---- datacenter -----------------------------------------------------------

def _api(path: str):
    key = os.environ.get("WEBSHARE_API_KEY")
    if not key:
        return None
    req = urllib.request.Request(
        f"https://proxy.webshare.io/api/v2/{path}",
        headers={"Authorization": f"Token {key}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _cache_path(kind):
    return os.path.join(VAR, f"webshare_{kind}.json")


def _session_path(kind):
    return os.path.join(VAR, f"{kind}_session.json")


def _plan_id(subtype: str):
    """Active plan id for a product subtype ('default', 'isp')."""
    plans = _api("subscription/plan/")
    if not plans:
        return None
    for p in plans.get("results", []):
        if p.get("status") == "active" and p.get("proxy_subtype") == subtype:
            return p["id"]
    return None


def _load_cache(kind):
    try:
        with open(_cache_path(kind)) as f:
            data = json.load(f)
        if time.time() - data.get("fetched_at", 0) < _CACHE_TTL and data.get("proxies"):
            return data
    except Exception:
        pass
    return None


def _refresh(kind):
    """Pull one product's proxy list from Webshare and cache it.

    `plan_id` is the filter that actually works. `proxy_type` and `plan` are
    both silently ignored and return the *other* plan's proxies, which is how an
    afternoon got spent believing the ISP IPs had never been provisioned.
    Each list entry carries its own username/password, so no separate config
    fetch is needed.
    """
    try:
        pid = _plan_id(_SUBTYPE[kind])
        if not pid:
            return None
        lst = _api(f"proxy/list/?mode=direct&plan_id={pid}&page_size=100")
    except Exception:
        return None
    if not lst:
        return None
    data = {
        "fetched_at": time.time(),
        "plan_id": pid,
        "proxies": [
            {"addr": p["proxy_address"], "port": p["port"], "cc": p.get("country_code"),
             "asn": p.get("asn_name"), "user": p["username"], "pass": p["password"]}
            for p in lst.get("results", [])
            if p.get("valid", True)
        ],
    }
    os.makedirs(VAR, exist_ok=True)
    tmp = _cache_path(kind) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)          # the file holds proxy passwords
    os.replace(tmp, _cache_path(kind))
    return data


def _session_index(kind: str, rotate_now: bool = False) -> int:
    """Which IP of the pool this product currently sits on.

    Held steady across launches so a session keeps one exit IP, bumped only on
    an explicit rotate. For `static` this is what makes the identity coherent:
    IP-bound `cf_clearance`/`datadome` cookies need the IP to stay put.
    """
    idx = 0
    try:
        with open(_session_path(kind)) as f:
            idx = int(json.load(f).get("index", 0))
    except Exception:
        pass
    if rotate_now:
        idx += 1
        os.makedirs(VAR, exist_ok=True)
        with open(_session_path(kind), "w") as f:
            json.dump({"index": idx, "since": time.time()}, f)
    return idx


def _from_pool(kind: str, rotate_now: bool = False):
    data = _load_cache(kind) or _refresh(kind)
    if not data or not data.get("proxies"):
        return None
    pool = data["proxies"]
    p = pool[_session_index(kind, rotate_now) % len(pool)]
    return {"server": f"http://{p['addr']}:{p['port']}", "username": p["user"],
            "password": p["pass"], "_asn": p.get("asn")}


def _datacenter(rotate_now: bool = False):
    return _from_pool(DATACENTER, rotate_now)


def _static(rotate_now: bool = False):
    """Static residential for identity. Falls back to the old rotating backbone
    if the ISP plan is gone, so a lapsed plan degrades rather than breaks."""
    return _from_pool(STATIC, rotate_now) or _residential()


# ---- public ---------------------------------------------------------------

def _resolve(kind: str | None, rotate_now: bool = False):
    """Internal: may carry `_asn` and other annotations for humans."""
    if kind is None or _disabled():
        return None
    if kind == RESIDENTIAL:
        return _residential()
    if kind == DATACENTER:
        return _datacenter(rotate_now=rotate_now)
    if kind == STATIC:
        return _static(rotate_now=rotate_now)
    raise ValueError(f"unknown proxy kind {kind!r}")


def build_proxy(kind: str | None, rotate_now: bool = False):
    """Patchright `proxy=` dict for a mode's egress, or None for direct.

    Annotations are stripped here: Patchright rejects unknown keys, so the
    launcher only ever sees server/username/password.

    None when the proxy is explicitly disabled or credentials are absent: the
    browser still boots and `/health` reports the fact, rather than the service
    failing to start.
    """
    p = _resolve(kind, rotate_now)
    return {k: v for k, v in p.items() if not k.startswith("_")} if p else None


def rotate(kind: str) -> bool:
    """Move to a different exit IP.

    Cheap for datacenter. Expensive for `static`: it invalidates every IP-bound
    cookie in the identity profile, so it is a deliberate act (burning a
    flagged IP), not routine hygiene.
    """
    if kind in (DATACENTER, STATIC):
        _session_index(kind, rotate_now=True)
        return True
    return False


def proxy_label(kind: str | None = None) -> str:
    """Human string for the panel and /health. Never leaks the password."""
    if kind is None:
        return "direct (no proxy)"
    p = _resolve(kind)
    if not p:
        reason = "disabled" if _disabled() else "no credentials"
        return f"direct ({kind}: {reason})"
    asn = p.get("_asn")
    return f"{kind} via {p['server']}" + (f" ({asn})" if asn else "")


def _auth_url(p: dict) -> str:
    host = p["server"].split("://", 1)[1]
    return f"http://{quote(p['username'], safe='')}:{quote(p['password'], safe='')}@{host}"


def _probe(p, timeout=20):
    handler = urllib.request.ProxyHandler({"http": _auth_url(p), "https": _auth_url(p)})
    opener = urllib.request.build_opener(handler)
    # Plain HTTP on purpose: an exhausted residential plan replaces the body
    # with its "Bandwidth limit reached" notice rather than failing outright.
    with opener.open("http://api.ipify.org", timeout=timeout) as r:
        return r.read(200).decode("utf-8", "replace").strip()


def egress_status(kind: str | None) -> dict:
    """Is this egress actually usable, and what IP does it exit from?

    Distinguishes 'configured' from 'has bandwidth left', the distinction that
    cost us an afternoon, since an exhausted plan looks configured and even
    works intermittently.
    """
    if kind is None or _disabled():
        return {"ok": True, "detail": "direct, no proxy", "exit_ip": None}
    p = build_proxy(kind)
    if not p:
        return {"ok": False, "detail": f"{kind}: no credentials", "exit_ip": None}
    try:
        body = _probe(p)
    except Exception as exc:
        return {"ok": False, "detail": f"{kind} unreachable: {exc}", "exit_ip": None}
    if "bandwidth" in body.lower():
        return {"ok": False, "detail": f"{kind} bandwidth exhausted", "exit_ip": None}
    if not body or " " in body:
        return {"ok": False, "detail": f"{kind} unexpected response: {body[:60]!r}", "exit_ip": None}
    return {"ok": True, "detail": f"{kind} ok", "exit_ip": body}
