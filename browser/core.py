"""BrowserManager: bounded parallel tabs in one persistent Patchright context.

Jobs share cookies and egress, but never pages, refs or byte meters. Mode
changes wait for all running tabs; explicit tab IDs preserve multi-call flows.

Four things here are load-bearing and not obvious from the code:

- **A job must name its mode** (see `modes.py`). Modes bundle exit IP, cookie jar
  and fingerprint; switching mode relaunches Chrome, because Patchright fixes
  both proxy and profile at `launch_persistent_context` time. Consumers should
  batch work by mode. There is deliberately no default.

- **Failures are soft.** A step that throws returns everything that succeeded
  before it, the index that failed, and an automatic snapshot of where the page
  ended up. The old behaviour (discard everything, return a timeout string)
  left an agent with no way to recover, which is the whole difference between a
  tool a weak model can drive and one it cannot.

- **Filtering must preserve the HTTP cache.** Playwright routing disables it,
  so CDP Fetch filters requests without page.route(). Identity blocks images
  unless the job opts in, plus known third-party trackers, media and fonts.

- **The UA is not pinned.** It used to claim Chrome 126 while the binary was
  150, a mismatch every fingerprinting script cross-checks against the Client
  Hints and the JS feature surface. An honest Chrome reports itself.
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import modes, proxy, snapshot
from .fallback import FallbackBudget

VAR = os.environ.get("GBROWSER_VAR") or os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "var")
SHOT_DIR = os.path.join(VAR, "shots")
DOWNLOAD_DIR = os.path.join(VAR, "downloads")
# Uploads are restricted to this directory. Without the restriction anything
# holding the service token could attach the instance passport (gp-core/.env)
# to a web form and exfiltrate it in a single call.
UPLOAD_DIR = os.path.realpath(os.environ.get("GBROWSER_UPLOAD_DIR", os.path.join(VAR, "uploads")))

MAX_SHOTS = 200

# Real Chrome, not bundled Chromium, is what actually beats DataDome. Headful
# is mandatory: headless is a giveaway. These args match a launch an earlier
# bot already proved.
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=9222",
]

_BLOCK_NORMAL = {"media", "font"}
_BLOCK_STRICT = {"media", "font", "image"}

_UA_OVERRIDE = os.environ.get("GBROWSER_UA")  # unset: let Chrome report itself

# A job that never returns holds a tab forever, so every job gets a
# wall-clock budget and every step a ceiling. Found the hard way: a Cloudflare
# interstitial that keeps re-navigating blocks `frame.evaluate` indefinitely,
# and one such page wedged the service for four minutes before it was killed.
JOB_TIMEOUT = float(os.environ.get("GBROWSER_JOB_TIMEOUT", "150"))
JOB_TIMEOUT_MAX = float(os.environ.get("GBROWSER_JOB_TIMEOUT_MAX", "900"))
STEP_TIMEOUT = float(os.environ.get("GBROWSER_STEP_TIMEOUT", "60"))
NAV_TIMEOUT_MS = float(os.environ.get("GBROWSER_NAV_TIMEOUT_MS", "30000"))
IDLE_TAB_SECONDS = 60

_TRACKERS = {
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googlesyndication.com", "googleadservices.com", "demdex.net", "tealiumiq.com",
    "trkn.us", "bat.bing.com", "connect.facebook.net", "hotjar.com",
    "hotjar.io", "clarity.ms", "segment.io", "segment.com", "amplitude.com",
    "mixpanel.com", "analytics.tiktok.com", "adsrvr.org", "criteo.com",
    "criteo.net", "taboola.com", "outbrain.com", "scorecardresearch.com",
    "quantserve.com", "adnxs.com", "rubiconproject.com", "pubmatic.com",
    "cloudflareinsights.com",
}
_CHALLENGES = {
    "datadome.co", "datadome.com", "captcha-delivery.com", "cloudflare.com",
    "hcaptcha.com", "recaptcha.net", "arkoselabs.com",
    "perimeterx.net", "px-cloud.net", "px-cdn.net", "awswaf.com",
}


def _domain(host, domains):
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _site(host):
    parts = host.split(".")
    count = 3 if host.endswith((".co.uk", ".org.uk", ".com.au", ".co.nz")) else 2
    return ".".join(parts[-count:])


def _blocked(url, resource_type, site_url, identity, frugal, images):
    host = _host(url)
    path = urlparse(url).path.lower()
    if (_domain(host, _CHALLENGES) or
            (_domain(host, {"google.com", "gstatic.com"}) and "/recaptcha/" in path) or
            "/cdn-cgi/challenge-platform/" in path):
        return False
    kind = resource_type.lower()
    if identity:
        if kind in {"media", "font", "ping"} or (kind == "image" and not images):
            return True
        tracker = _domain(host, _TRACKERS) or (_domain(host, {"facebook.com"}) and path.startswith("/tr"))
        return _site(host) != _site(_host(site_url)) and tracker
    block = _BLOCK_STRICT if str(frugal).lower() == "strict" else (_BLOCK_NORMAL if frugal else set())
    return kind in block


@dataclass
class Tab:
    page: object
    cdp: object
    frame_id: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sigs: dict = field(default_factory=dict)
    holder: str | None = None
    active_holder: str | None = None
    since: float | None = None
    last_used: float = field(default_factory=time.monotonic)
    last_status: int | None = None
    site_url: str = "about:blank"
    frugal: object = True
    images: bool = False
    job: dict | None = None
    requests: dict = field(default_factory=dict)
    auth_attempts: set = field(default_factory=set)


class Activity:
    """Ring buffer of what the browser did, plus live fan-out to the panel.

    The VNC view shows the pixels; this is the intent. Watching an agent work
    means seeing both. A screen recording alone is close to useless for
    understanding why something went wrong.
    """

    def __init__(self, size=500):
        self.events = deque(maxlen=size)
        self._subs: set[asyncio.Queue] = set()

    def add(self, **ev):
        ev["ts"] = time.time()
        self.events.append(ev)
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs.add(q)
        return q

    def unsubscribe(self, q):
        self._subs.discard(q)

    def recent(self, n=100):
        return list(self.events)[-n:]


activity = Activity()


def _safe_upload(path: str) -> str:
    rp = os.path.realpath(path)
    if rp != UPLOAD_DIR and not rp.startswith(UPLOAD_DIR + os.sep):
        raise ValueError(f"upload refused: files must live under {UPLOAD_DIR}")
    if not os.path.isfile(rp):
        raise ValueError(f"no such file: {path}")
    return rp


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"


def _prune(d: str, keep: int):
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getmtime, reverse=True)
        for f in files[keep:]:
            os.remove(f)
    except Exception:
        pass


class BrowserManager:
    def __init__(self):
        self._pw = None
        self._ctx = None
        self._page = None
        self._proxy = None
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._tabs = {}
        self._waiters = []
        self._idle_task = None
        self.max_tabs = int(os.environ.get("GBROWSER_MAX_TABS", "3"))
        if self.max_tabs < 1:
            raise ValueError("GBROWSER_MAX_TABS must be a positive integer")
        self.attached = False
        self.last_error = None
        self.started_at = None
        self.mode: modes.Mode | None = None
        self._bytes: dict[str, dict[str, int]] = {}
        self._last_job = None
        self.fallback = FallbackBudget(os.path.join(VAR, "fallback.sqlite3"))

    # ---- lifecycle -------------------------------------------------------

    async def start(self, mode_name: str | None = None):
        """Launch the persistent context in a mode. Never raises: a failure
        (e.g. no system Chrome) is recorded so /health can report it and the API
        still serves."""
        try:
            wanted = mode_name or (self.mode.name if self.mode else None) or modes.RESEARCH.name
            mode = modes.resolve(wanted)
        except ValueError as exc:
            self.last_error = str(exc)
            return
        try:
            from patchright.async_api import async_playwright
        except Exception as exc:  # patchright not installed yet
            self.last_error = f"patchright import failed: {exc}"
            return
        try:
            os.makedirs(mode.profile, exist_ok=True)
            for d in (SHOT_DIR, DOWNLOAD_DIR, UPLOAD_DIR):
                os.makedirs(d, exist_ok=True)

            self._proxy = proxy.build_proxy(mode.proxy_kind)
            kwargs = dict(
                headless=False,
                channel="chrome",
                proxy=self._proxy,
                viewport=mode.viewport,
                locale=mode.locale,
                timezone_id=mode.timezone,
                accept_downloads=True,
                args=CHROME_ARGS + [f"--window-size={mode.viewport['width']},{mode.viewport['height']}",
                                    f"--disk-cache-dir={os.path.join(mode.profile, 'http-cache')}",
                                    "--disk-cache-size=268435456"],
            )
            if _UA_OVERRIDE:
                kwargs["user_agent"] = _UA_OVERRIDE

            self._pw = await async_playwright().start()
            self._ctx = await self._pw.chromium.launch_persistent_context(mode.profile, **kwargs)
            self.mode = mode
            pages = self._ctx.pages
            page = pages[0] if pages else await self._ctx.new_page()
            for extra in pages[1:]:
                await extra.close()
            await self._new_tab(page)
            self.attached = True
            self.last_error = None
            self.started_at = time.time()
            activity.add(kind="mode", mode=mode.name, detail=mode.label)
        except Exception as exc:
            self.last_error = f"launch failed: {exc}"
            self.attached = False

    async def _new_tab(self, page=None):
        page = page or await self._ctx.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page.set_default_timeout(NAV_TIMEOUT_MS)
        cdp = await self._ctx.new_cdp_session(page)
        tab = Tab(page, cdp)
        tab.frame_id = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
        self._tabs[tab.id] = tab
        self._page = page
        cdp.on("Network.requestWillBeSent", lambda ev: self._request(tab, ev))
        cdp.on("Network.responseReceived", lambda ev: self._response(tab, ev))
        cdp.on("Network.dataReceived", lambda ev: self._data(tab, ev))
        cdp.on("Network.loadingFinished", lambda ev: self._finished(tab, ev))
        cdp.on("Network.loadingFailed", lambda ev: tab.requests.pop(ev["requestId"], None))
        cdp.on("Fetch.requestPaused", lambda ev: self._filter(tab, ev))
        cdp.on("Fetch.authRequired", lambda ev: self._authenticate(tab, ev))
        await cdp.send("Network.enable")
        await cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
        await cdp.send("Fetch.enable", {"handleAuthRequests": True,
                                        "patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        return tab

    async def _authenticate(self, tab, ev):
        # The filtering session owns proxy challenges once Fetch is enabled.
        response = {"response": "Default"}
        if ev["authChallenge"]["source"] == "Proxy" and self._proxy:
            if ev["requestId"] in tab.auth_attempts:
                response = {"response": "CancelAuth"}
            else:
                tab.auth_attempts.add(ev["requestId"])
                response = {"response": "ProvideCredentials",
                            "username": self._proxy.get("username", ""),
                            "password": self._proxy.get("password", "")}
        try:
            await tab.cdp.send("Fetch.continueWithAuth", {"requestId": ev["requestId"],
                                                        "authChallengeResponse": response})
        except Exception:
            pass

    async def _filter(self, tab, ev):
        if ev["resourceType"] == "Document" and ev.get("frameId") == tab.frame_id:
            tab.site_url = ev["request"]["url"]
        blocked = _blocked(ev["request"]["url"], ev["resourceType"], tab.site_url,
                           self.mode.identity, tab.frugal, tab.images)
        method = "Fetch.failRequest" if blocked else "Fetch.continueRequest"
        params = {"requestId": ev["requestId"]}
        if blocked:
            params["errorReason"] = "BlockedByClient"
        try:
            await tab.cdp.send(method, params)
        except Exception:
            pass  # A navigation or tab close can cancel an intercepted request.

    def _request(self, tab, ev):
        if ev.get("redirectResponse"):
            previous = tab.requests.get(ev["requestId"], {})
            self._credit(tab, ev["requestId"], ev["redirectResponse"].get("encodedDataLength", 0) - previous.get("bytes", 0))
        tab.requests[ev["requestId"]] = {
            "host": _host(ev["request"]["url"]), "type": ev.get("type", "Other"),
            "job": tab.job, "bytes": 0, "cached": False,
        }

    def _response(self, tab, ev):
        response = ev["response"]
        req = tab.requests.get(ev["requestId"])
        if req is not None:
            req["cached"] = bool(response.get("fromDiskCache") or response.get("fromServiceWorker"))
        if ev.get("type") == "Document" and ev.get("frameId") == tab.frame_id:
            tab.last_status = response["status"]
            tab.site_url = response["url"]

    def _credit(self, tab, request_id, n):
        req = tab.requests.get(request_id)
        if not req or req["cached"] or n <= 0:
            return
        n = int(n)
        req["bytes"] += n
        hosts = self._bytes.setdefault(self.mode.name, {})
        hosts[req["host"]] = hosts.get(req["host"], 0) + n
        job = req["job"]
        if job is not None and job is tab.job:
            job["bytes"] += n
            for key, value in (("by_host", req["host"]), ("by_resource_type", req["type"])):
                job[key][value] = job[key].get(value, 0) + n

    def _data(self, tab, ev):
        self._credit(tab, ev["requestId"], ev.get("encodedDataLength", 0))

    def _finished(self, tab, ev):
        req = tab.requests.get(ev["requestId"])
        if req:
            self._credit(tab, ev["requestId"], ev.get("encodedDataLength", 0) - req["bytes"])
            tab.requests.pop(ev["requestId"], None)

    @property
    def holder(self):
        return ", ".join(t.active_holder for t in self._tabs.values() if t.since is not None) or None

    @property
    def holder_since(self):
        return min((t.since for t in self._tabs.values() if t.since is not None), default=None)

    def _busy(self):
        return any(t.since is not None for t in self._tabs.values())

    async def stop(self):
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._ctx = self._page = self._pw = None
            self.attached = False
            self._tabs.clear()

    async def restart(self, mode_name: str | None = None):
        """Tear down and relaunch after a login, a config change, or a mode
        switch. Refuses while any tab is busy."""
        async with self._condition:
            if self._busy():
                return {"ok": False, "error": "busy: jobs hold tabs"}
            return await self._relaunch(mode_name)

    async def _relaunch(self, mode_name: str | None):
        """Relaunch while holding the scheduler lock, after every busy tab drains."""
        await self.stop()
        await self.start(mode_name)
        return {"ok": self.attached, "error": self.last_error,
                "mode": self.mode.name if self.mode else None}

    async def reseed(self, tar_bytes: bytes | None, mode_name: str | None = None):
        """Replace a mode's persistent profile: wipe it, optionally extract an
        uploaded profile export into it, relaunch.

        The usual seed path is interactive: drive the live browser via noVNC and
        log in; the persistent context keeps it. This is the from-export
        alternative."""
        import io
        import shutil
        import tarfile
        async with self._condition:
            if self._busy():
                return {"ok": False, "error": "busy: jobs hold tabs"}
            try:
                mode = modes.resolve(mode_name or (self.mode.name if self.mode else None))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            await self.stop()
            shutil.rmtree(mode.profile, ignore_errors=True)
            os.makedirs(mode.profile, exist_ok=True)
            if tar_bytes:
                try:
                    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
                        tar.extractall(mode.profile, filter="data")  # data filter blocks path traversal
                except Exception as exc:
                    await self.start(mode.name)
                    return {"ok": False, "error": f"bad profile archive: {exc}"}
            await self.start(mode.name)
            activity.add(kind="reseed", mode=mode.name,
                         detail=f"profile replaced ({'from export' if tar_bytes else 'wiped'})")
            return {"ok": self.attached, "error": self.last_error, "mode": mode.name}

    # ---- introspection ---------------------------------------------------

    def status(self):
        return {
            "attached": self.attached,
            "mode": self.mode.name if self.mode else None,
            "identity": bool(self.mode and self.mode.identity),
            "proxy": proxy.proxy_label(self.mode.proxy_kind if self.mode else None),
            "profile": self.mode.profile if self.mode else None,
            "busy": self._busy(),
            "tabs": {"busy": sum(t.since is not None for t in self._tabs.values()),
                     "total": len(self._tabs), "max": self.max_tabs,
                     "sessions": [{"tab_id": t.id, "holder": t.holder,
                                   "busy": t.since is not None, "holder_since": t.since,
                                   "active_holder": t.active_holder if t.since is not None else None,
                                   "url": t.page.url} for t in self._tabs.values()]},
            "holder": self.holder,
            "holder_since": self.holder_since,
            "current_url": (self._page.url if self._page else None),
            "last_error": self.last_error,
            "last_http_status": next((t.last_status for t in self._tabs.values() if t.page is self._page), None),
            "started_at": self.started_at,
            "bandwidth": self.bandwidth(),
            "last_job": self._last_job,
            "fallback": self.fallback.status(),
            "modes": modes.describe(),
        }

    def bandwidth(self):
        return {
            m: {"total": sum(h.values()),
                "by_host": dict(sorted(h.items(), key=lambda kv: -kv[1])[:15])}
            for m, h in self._bytes.items()
        }

    # ---- job execution ---------------------------------------------------

    async def _claim(self, mode, holder, tab_id, navigates):
        ticket = object()
        async with self._condition:
            self._waiters.append(ticket)
            try:
                while True:
                    if self._waiters[0] is not ticket:
                        await self._condition.wait()
                        continue
                    switched = None
                    if not self.mode or self.mode.name != mode.name:
                        if tab_id:
                            raise ValueError("tab belongs to another mode or expired; start a new session")
                        if self._busy():
                            await self._condition.wait()
                            continue
                        result = await self._relaunch(mode.name)
                        switched = mode.name
                        if not result["ok"]:
                            raise ValueError(f"could not switch to {mode.name}: {result['error']}")
                    if not self.attached:
                        raise ValueError(self.last_error or "browser not attached")
                    for t in list(self._tabs.values()):
                        if t.page.is_closed():
                            self._tabs.pop(t.id)
                    if tab_id:
                        tab = self._tabs.get(tab_id)
                        if tab is None or tab.holder != holder:
                            raise ValueError("unknown, expired or other consumer's tab_id; take a new snapshot")
                        if tab.since is not None:
                            await self._condition.wait()
                            continue
                    else:
                        owned = [t for t in self._tabs.values() if t.holder == holder]
                        # Existing assist polls the current page under its own label.
                        if holder == "assist" and not owned and not navigates:
                            owned = list(self._tabs.values())
                        if not navigates and len(owned) > 1:
                            raise ValueError("multiple tabs for this consumer; pass tab_id from the snapshot")
                        idle = [t for t in owned if t.since is None]
                        tab = max(idle, key=lambda t: t.last_used) if idle else None
                        if tab is None and owned and not navigates:
                            await self._condition.wait()
                            continue
                        if tab is None:
                            tab = next((t for t in self._tabs.values() if t.holder is None), None)
                        if tab is None:
                            if len(self._tabs) >= self.max_tabs:
                                idle = [t for t in self._tabs.values() if t.since is None]
                                if not idle:
                                    await self._condition.wait()
                                    continue
                                await self._close_tab(min(idle, key=lambda t: t.last_used))
                            tab = await self._new_tab()
                    if holder != "assist" or tab.holder is None:
                        tab.holder = holder
                    tab.active_holder = holder
                    tab.since = time.time()
                    self._page = tab.page
                    return tab, switched
            finally:
                self._waiters.remove(ticket)
                self._condition.notify_all()

    async def _close_tab(self, tab):
        await tab.page.close()
        self._tabs.pop(tab.id, None)
        if self._page is tab.page:
            self._page = next((t.page for t in self._tabs.values()), None)

    async def _reap_idle(self):
        try:
            while len(self._tabs) > 1:
                now = time.monotonic()
                delay = min((t.last_used + IDLE_TAB_SECONDS - now for t in self._tabs.values()
                             if t.since is None), default=IDLE_TAB_SECONDS)
                await asyncio.sleep(max(.01, delay))
                async with self._condition:
                    for tab in sorted(self._tabs.values(), key=lambda t: t.last_used):
                        if len(self._tabs) > 1 and tab.since is None and time.monotonic() - tab.last_used >= IDLE_TAB_SECONDS:
                            await self._close_tab(tab)
                    self._condition.notify_all()
        finally:
            self._idle_task = None

    async def run(self, job: dict, holder: str = "unknown") -> dict:
        """Lease one tab; follow-up jobs address it with tab_id (API params.tab_id)."""
        try:
            mode = modes.resolve(job.get("mode"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "modes": modes.describe()}
        steps = job.get("steps")
        if steps is None:
            steps = [{k: v for k, v in job.items() if k not in
                      ("url", "frugal", "mode", "snapshot_on_error", "fallback", "tab_id", "images")}]
        frugal = job.get("frugal", True)
        fallback = job.get("fallback", False)
        if fallback:
            labelled = any(step.get("action") == "screenshot" and step.get("label_refs") for step in steps)
            frugal = True if labelled else "strict"
        budget = min(float(job.get("timeout_s", JOB_TIMEOUT)), JOB_TIMEOUT_MAX)
        job_id = None
        if fallback and not mode.identity:
            reservation = self.fallback.reserve(mode.name, holder)
            if not reservation["ok"]:
                return reservation
            job_id = reservation["job_id"]
        tab = None
        result = None
        meter = {"bytes": 0, "by_host": {}, "by_resource_type": {}}
        try:
            tab, switched = await self._claim(mode, holder, job.get("tab_id"),
                                             bool(job.get("url")) or any(s.get("action") == "goto" for s in steps))
            tab.job = meter
            tab.auth_attempts.clear()
            tab.frugal = frugal
            tab.images = job.get("images") is True
            activity.add(kind="job_start", mode=mode.name, holder=holder, tab_id=tab.id)
            want_snapshot = job.get("snapshot_on_error", True)
            try:
                result = await asyncio.wait_for(
                    self._run_steps(steps, job.get("url"), holder, mode, switched, want_snapshot, tab),
                    timeout=budget,
                )
            except asyncio.TimeoutError:
                result = await self._failure(TimeoutError(f"job exceeded its {budget}s budget"),
                                             -1, [], holder, mode, switched, want_snapshot, tab)
            return result
        except ValueError as exc:
            result = {"ok": False, "error": str(exc)}
            return result
        finally:
            if tab is not None:
                tab.job = None
                self._last_job = {"mode": mode.name, "holder": holder, "tab_id": tab.id,
                                  "started_at": tab.since, "finished_at": time.time(),
                                  "fallback": bool(fallback), "bytes": meter["bytes"],
                                  "ok": bool(result and result.get("ok"))}
                if result is not None:
                    result.update(bytes=meter["bytes"], bandwidth=meter, tab_id=tab.id,
                                  started_at=tab.since, finished_at=self._last_job["finished_at"])
                activity.add(kind="job_end", **self._last_job)
                async with self._condition:
                    tab.since = None
                    tab.last_used = time.monotonic()
                    self._condition.notify_all()
                    if len(self._tabs) > 1 and self._idle_task is None:
                        self._idle_task = asyncio.create_task(self._reap_idle())
            elif result is not None:
                result["bytes"] = 0
            if job_id is not None:
                self.fallback.finish(job_id, meter["bytes"], bool(result and result.get("ok")))

    async def _run_steps(self, steps, url, holder, mode, switched, want_snapshot, tab):
        results = []
        try:
            if url:
                activity.add(kind="action", mode=mode.name, holder=holder, action="goto", detail=url)
                tab.site_url = url
                await tab.page.goto(url, wait_until="domcontentloaded")

            for i, step in enumerate(steps):
                try:
                    ceiling = max(STEP_TIMEOUT, float(step.get("timeout", 0)) / 1000 + 10)
                    value = await asyncio.wait_for(self._do(step, tab), timeout=ceiling)
                    results.append(value)
                    activity.add(kind="action", mode=mode.name, holder=holder,
                                 action=step.get("action"), detail=self._describe(step, value))
                except Exception as exc:
                    # asyncio.wait_for raises a bare TimeoutError with an empty
                    # message; say what actually happened instead.
                    if isinstance(exc, asyncio.TimeoutError) and not str(exc):
                        exc = TimeoutError(f"step {i} ({step.get('action')}) exceeded {ceiling}s")
                    if step.get("optional"):
                        results.append({"skipped": True, "error": str(exc)})
                        activity.add(kind="skip", mode=mode.name, holder=holder,
                                     action=step.get("action"), detail=str(exc)[:160])
                        continue
                    activity.add(kind="error", mode=mode.name, holder=holder,
                                 action=step.get("action"), detail=str(exc)[:200])
                    return await self._failure(exc, i, results, holder, mode, switched, want_snapshot, tab)

            out = {"ok": True, "mode": mode.name, "results": results,
                   "holder": holder, "url": tab.page.url}
            if switched:
                out["switched_mode_to"] = switched
            challenge = await snapshot.detect(tab.page)
            if challenge:
                out["challenge"] = challenge
            return out
        except Exception as exc:
            return await self._failure(exc, len(results), results, holder, mode, switched, want_snapshot, tab)

    async def _failure(self, exc, index, results, holder, mode, switched, want_snapshot, tab):
        """Fail soft: everything that worked, where it broke, and what the page
        looks like now, so the caller can decide rather than guess."""
        state = {"url": tab.page.url, "http_status": tab.last_status}
        # Diagnostics must not themselves hang. We are often here *because* the
        # page is misbehaving, so everything below is on a short leash.
        try:
            state["challenge"] = await asyncio.wait_for(snapshot.detect(tab.page), timeout=25)
        except Exception:
            state["challenge"] = None
        if want_snapshot:
            try:
                snap = await asyncio.wait_for(
                    snapshot.take(tab.page, max_elements=200), timeout=40
                )
                tab.sigs.update(snap.pop("signatures", {}))
                state["snapshot"] = snap["snapshot"]
            except Exception as sexc:
                state["snapshot_error"] = str(sexc) or type(sexc).__name__
        out = {
            "ok": False,
            "mode": mode.name,
            "error": str(exc),
            "failed_step": index,
            "results": results,
            "state": state,
            "holder": holder,
            "url": state["url"],
        }
        if switched:
            out["switched_mode_to"] = switched
        if state.get("challenge"):
            out["challenge"] = state["challenge"]
        return out

    @staticmethod
    def _describe(step, value):
        """One-line human summary for the activity feed."""
        a = step.get("action")
        target = step.get("ref") or step.get("selector") or ""
        if a == "snapshot" and isinstance(value, dict):
            return f"{value.get('refs')} refs across {len(value.get('frames', {}))} frame(s)"
        if a in ("fill", "type") and isinstance(step.get("value"), str):
            v = step["value"]
            return f'{target} ← "{v[:40]}{"…" if len(v) > 40 else ""}"'
        if a == "screenshot" and isinstance(value, dict):
            return value.get("url", "")
        if a == "read_text" and isinstance(value, dict):
            t = (value.get("text") or "").replace("\n", " ")[:60]
            return f'{target} → "{t}…"' if t else target
        if a == "check_challenge" and isinstance(value, dict):
            c = value.get("challenge")
            return f"{c['kind']} ({c['confidence']})" if c else "clear"
        return target or ""

    # ---- targeting -------------------------------------------------------

    async def _locate(self, step, tab):
        """Resolve a step's target to an ElementHandle.

        `ref` (from a snapshot) wins over `selector`. Refs fail closed (see
        snapshot.resolve) because clicking the wrong control on a real job
        application is the worst thing this service can do.
        """
        ref = step.get("ref")
        if ref:
            return await snapshot.resolve(tab.page, ref, {}, tab.sigs)
        sel = step.get("selector")
        if not sel:
            raise ValueError("step needs a `ref` (from a snapshot) or a `selector`")
        el = await tab.page.wait_for_selector(
            sel, timeout=step.get("timeout", 15000), state=step.get("state", "visible")
        )
        if el is None:
            raise ValueError(f"selector matched nothing: {sel}")
        return el

    async def _type_human(self, el, text, base=None):
        """Type with per-word jitter.

        Playwright's `delay=` is a fixed inter-key interval, which is itself a
        signature: real typing is bursty, quick inside words and hesitant
        between them. Cheap to fix here so every consumer inherits it.
        """
        await el.click()
        for i, word in enumerate(text.split(" ")):
            chunk = (" " if i else "") + word
            await el.type(chunk, delay=base if base is not None else random.uniform(38, 95))
            await asyncio.sleep(random.uniform(0.02, 0.16))

    # ---- the action set --------------------------------------------------

    async def _do(self, step: dict, tab):
        """Execute one generic action. Returns a JSON-serialisable value."""
        action = step.get("action")
        p = tab.page

        # --- perception ---
        if action == "snapshot":
            snap = await snapshot.take(
                p, max_elements=int(step.get("max_elements", 400)), within=step.get("within")
            )
            tab.sigs.update(snap.pop("signatures", {}))
            return snap
        if action == "check_challenge":
            return {"challenge": await snapshot.detect(p), "http_status": tab.last_status}

        # --- navigation ---
        if action == "goto":
            tab.site_url = step["url"]
            await p.goto(step["url"], wait_until=step.get("wait_until", "domcontentloaded"))
            return {"url": p.url}
        if action == "back":
            await p.go_back(wait_until=step.get("wait_until", "domcontentloaded"))
            return {"url": p.url}
        if action == "reload":
            await p.reload(wait_until=step.get("wait_until", "domcontentloaded"))
            return {"url": p.url}

        # --- interaction ---
        if action == "click":
            el = await self._locate(step, tab)
            await el.scroll_into_view_if_needed(timeout=step.get("timeout", 15000))
            await el.click(timeout=step.get("timeout", 15000))
            return {"clicked": step.get("ref") or step.get("selector")}
        if action == "hover":
            el = await self._locate(step, tab)
            await el.hover(timeout=step.get("timeout", 15000))
            return {"hovered": step.get("ref") or step.get("selector")}
        if action == "focus":
            el = await self._locate(step, tab)
            await el.focus()
            return {"focused": step.get("ref") or step.get("selector")}
        if action == "fill":
            el = await self._locate(step, tab)
            await el.fill(step["value"], timeout=step.get("timeout", 15000))
            return {"filled": step.get("ref") or step.get("selector")}
        if action == "type":
            el = await self._locate(step, tab)
            await self._type_human(el, step["value"], step.get("delay"))
            return {"typed": step.get("ref") or step.get("selector")}
        if action == "clear":
            el = await self._locate(step, tab)
            await el.fill("")
            return {"cleared": step.get("ref") or step.get("selector")}
        if action == "press":
            key = step["key"]
            if step.get("ref") or step.get("selector"):
                el = await self._locate(step, tab)
                await el.press(key)
            else:
                await p.keyboard.press(key)
            return {"pressed": key}
        if action == "select_option":
            el = await self._locate(step, tab)
            by = step.get("by", "label")
            kwargs = {by: step["value"]} if by in ("label", "value", "index") else {"label": step["value"]}
            return {"selected": await el.select_option(**kwargs)}
        if action == "scroll":
            await p.mouse.wheel(0, int(step.get("dy", 600)))
            return {"scrolled": step.get("dy", 600)}
        if action == "scroll_into_view":
            el = await self._locate(step, tab)
            await el.scroll_into_view_if_needed()
            return {"scrolled_to": step.get("ref") or step.get("selector")}

        # --- files ---
        if action == "upload":
            files = [_safe_upload(f) for f in (step.get("files") or [step.get("file")]) if f]
            if not files:
                raise ValueError("upload needs `files`")
            if step.get("via") == "chooser":
                # Many ATS forms hide the real <input type=file> behind a styled
                # button; the chooser flow is the only way in.
                async with p.expect_file_chooser(timeout=step.get("timeout", 15000)) as info:
                    el = await self._locate(step, tab)
                    await el.click()
                chooser = await info.value
                await chooser.set_files(files)
            else:
                el = await self._locate(step, tab)
                await el.set_input_files(files)
            return {"uploaded": [os.path.basename(f) for f in files]}
        if action == "download":
            async with p.expect_download(timeout=step.get("timeout", 30000)) as info:
                el = await self._locate(step, tab)
                await el.click()
            dl = await info.value
            name = dl.suggested_filename or f"download-{int(time.time())}"
            dest = os.path.join(DOWNLOAD_DIR, name)
            await dl.save_as(dest)
            return {"downloaded": dest, "filename": name}

        # --- reading ---
        if action == "wait_for":
            await p.wait_for_selector(step["selector"], timeout=step.get("timeout", 15000),
                                      state=step.get("state", "visible"))
            return {"appeared": step["selector"]}
        if action == "wait":
            await p.wait_for_timeout(int(step.get("ms", 1000)))
            return {"waited_ms": step.get("ms", 1000)}
        if action == "read_text":
            if step.get("ref") or step.get("selector"):
                el = await self._locate(step, tab)
                return {"text": await el.inner_text()}
            return {"text": await p.inner_text("body")}
        if action == "read_html":
            if step.get("ref") or step.get("selector"):
                el = await self._locate(step, tab)
                return {"html": await el.inner_html()}
            return {"html": await p.content()}
        if action == "get_attribute":
            el = await self._locate(step, tab)
            return {"value": await el.get_attribute(step["name"])}
        if action == "query_all":
            els = await p.query_selector_all(step["selector"])
            return {"texts": [await e.inner_text() for e in els]}
        if action == "eval":
            return {"value": await p.evaluate(step["script"])}
        if action == "screenshot":
            return await self._screenshot(step, tab)

        raise ValueError(f"unknown action: {action!r}")

    async def _screenshot(self, step, tab):
        """Capture the page, optionally with ref badges drawn on.

        Labels are drawn inside each frame's own document rather than composited
        in the parent. The browser then does the compositing for us, which is
        what makes it correct at any iframe nesting depth.
        """
        label = bool(step.get("label_refs"))
        p = tab.page
        if label:
            snap = await snapshot.take(p, max_elements=int(step.get("max_elements", 400)))
            tab.sigs.update(snap.pop("signatures", {}))
            await snapshot.set_overlay(p, True)
        try:
            png = await p.screenshot(full_page=bool(step.get("full_page", False)))
        finally:
            if label:
                try:
                    await snapshot.set_overlay(p, False)
                except Exception:
                    pass

        os.makedirs(SHOT_DIR, exist_ok=True)
        name = f"{int(time.time() * 1000)}-{tab.id[:12]}.png"
        path = os.path.join(SHOT_DIR, name)
        with open(path, "wb") as f:
            f.write(png)
        _prune(SHOT_DIR, MAX_SHOTS)

        out = {"path": path, "url": f"/shots/{name}", "bytes": len(png), "labelled": label}
        # b64 only on request: a 1080p PNG is ~200KB, a large slice of an agent's
        # context to spend without being asked for it.
        if step.get("inline"):
            out["png_b64"] = base64.b64encode(png).decode()
        return out


manager = BrowserManager()
