"""BrowserManager: one always-on headful Patchright Chrome, serialised.

One browser, one job at a time, consumers taking turns. What changed in v2 is
what the browser can *tell* you: alongside the original selector-driven action
set (which an earlier bot's hard-won choreography still uses verbatim) it can
now snapshot a page into a numbered outline an agent acts on without knowing any
CSS, report when it has walked into a bot wall, and hand back partial results
plus the state it died in rather than a bare timeout string.

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

- **Frugal no longer blocks images.** Measured on LinkedIn: images are ~180KB of
  a 2.1MB page load, so blocking them saved 9% while making every screenshot
  useless to a vision model. Media and fonts are still blocked (they are the
  actual hogs), and `frugal: "strict"` restores the old set for when residential
  bytes matter more than seeing the page.

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
from collections import deque
from urllib.parse import urlparse

from . import modes, proxy, snapshot

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

# A job that never returns holds the only browser forever, so every job gets a
# wall-clock budget and every step a ceiling. Found the hard way: a Cloudflare
# interstitial that keeps re-navigating blocks `frame.evaluate` indefinitely,
# and one such page wedged the service for four minutes before it was killed.
JOB_TIMEOUT = float(os.environ.get("GBROWSER_JOB_TIMEOUT", "150"))
JOB_TIMEOUT_MAX = float(os.environ.get("GBROWSER_JOB_TIMEOUT_MAX", "900"))
STEP_TIMEOUT = float(os.environ.get("GBROWSER_STEP_TIMEOUT", "60"))
NAV_TIMEOUT_MS = float(os.environ.get("GBROWSER_NAV_TIMEOUT_MS", "30000"))


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
        self._lock = asyncio.Lock()
        self.attached = False
        self.last_error = None
        self.holder = None          # which consumer holds the lock right now
        self.holder_since = None
        self.started_at = None
        self.mode: modes.Mode | None = None
        self._sigs: dict[str, str] = {}               # ref -> signature, last snapshot
        self._bytes: dict[str, dict[str, int]] = {}   # mode -> host -> bytes
        self._last_status = None                      # last main-frame document status

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

            kwargs = dict(
                headless=False,
                channel="chrome",
                proxy=proxy.build_proxy(mode.proxy_kind),
                viewport=mode.viewport,
                locale=mode.locale,
                timezone_id=mode.timezone,
                accept_downloads=True,
                args=CHROME_ARGS + [f"--window-size={mode.viewport['width']},{mode.viewport['height']}"],
            )
            if _UA_OVERRIDE:
                kwargs["user_agent"] = _UA_OVERRIDE

            self._pw = await async_playwright().start()
            self._ctx = await self._pw.chromium.launch_persistent_context(mode.profile, **kwargs)
            self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            self._page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            self._page.set_default_timeout(NAV_TIMEOUT_MS)
            self._ctx.on("response", self._on_response)
            self.mode = mode
            self.attached = True
            self.last_error = None
            self.started_at = time.time()
            self._sigs.clear()
            activity.add(kind="mode", mode=mode.name, detail=mode.label)
        except Exception as exc:
            self.last_error = f"launch failed: {exc}"
            self.attached = False

    def _on_response(self, response):
        """Estimate egress bytes per host, and remember the last document status.

        Content-Length is an estimate (chunked responses report nothing), but it
        is the only measurement available without buying the body, and it is the
        meter the governor will need to keep residential spend inside its plan.
        """
        try:
            if self.mode:
                n = int(response.headers.get("content-length") or 0)
                if n:
                    per_mode = self._bytes.setdefault(self.mode.name, {})
                    host = _host(response.url)
                    per_mode[host] = per_mode.get(host, 0) + n
            req = response.request
            if req.is_navigation_request() and req.frame.parent_frame is None:
                self._last_status = response.status
        except Exception:
            pass

    async def stop(self):
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._ctx = self._page = self._pw = None
            self.attached = False

    async def restart(self, mode_name: str | None = None):
        """Tear down and relaunch after a login, a config change, or a mode
        switch. Refuses while a job holds the lock."""
        if self._lock.locked():
            return {"ok": False, "error": "busy: a job holds the lock"}
        return await self._relaunch(mode_name)

    async def _relaunch(self, mode_name: str | None):
        """Unguarded relaunch. Callers must already hold the lock, or be certain
        nothing does. `restart` is the guarded entry point."""
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
        if self._lock.locked():
            return {"ok": False, "error": "busy: a job holds the lock"}
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
            "busy": self._lock.locked(),
            "holder": self.holder,
            "holder_since": self.holder_since,
            "current_url": (self._page.url if self._page else None),
            "last_error": self.last_error,
            "last_http_status": self._last_status,
            "started_at": self.started_at,
            "bandwidth": self.bandwidth(),
            "modes": modes.describe(),
        }

    def bandwidth(self):
        return {
            m: {"total": sum(h.values()),
                "by_host": dict(sorted(h.items(), key=lambda kv: -kv[1])[:15])}
            for m, h in self._bytes.items()
        }

    # ---- job execution ---------------------------------------------------

    async def run(self, job: dict, holder: str = "unknown") -> dict:
        """Run one job under the global lock.

        A job is `{mode, url?, steps:[...], frugal?}` or a single-action
        shorthand. `mode` is required. See modes.py for why there is no default.
        """
        try:
            mode = modes.resolve(job.get("mode"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "modes": modes.describe()}

        steps = job.get("steps")
        if steps is None:
            steps = [{k: v for k, v in job.items()
                      if k not in ("url", "frugal", "mode", "snapshot_on_error")}]
        frugal = job.get("frugal", True)
        url = job.get("url")
        want_snapshot = job.get("snapshot_on_error", True)
        budget = min(float(job.get("timeout_s", JOB_TIMEOUT)), JOB_TIMEOUT_MAX)

        async with self._lock:
            self.holder = holder
            self.holder_since = time.time()
            switched = None
            try:
                if not self.mode or self.mode.name != mode.name:
                    activity.add(kind="switch", mode=mode.name, holder=holder,
                                 detail=f"{self.mode.name if self.mode else 'none'} → {mode.name}")
                    r = await self._relaunch(mode.name)
                    switched = mode.name
                    if not r["ok"]:
                        return {"ok": False, "error": f"could not switch to {mode.name}: {r['error']}"}
                if not self.attached:
                    return {"ok": False, "error": self.last_error or "browser not attached"}
                try:
                    return await asyncio.wait_for(
                        self._run_steps(steps, url, frugal, holder, mode, switched, want_snapshot),
                        timeout=budget,
                    )
                except asyncio.TimeoutError:
                    # The lock is released by the `async with` regardless, which
                    # is the point: one wedged job must never take the shared
                    # browser down with it.
                    activity.add(kind="error", mode=mode.name, holder=holder,
                                 action="timeout", detail=f"job exceeded {budget}s")
                    return await self._failure(
                        TimeoutError(f"job exceeded its {budget}s budget, so the page may be stuck "
                                     "(a bot wall that keeps re-navigating does this)"),
                        -1, [], holder, mode, switched, want_snapshot,
                    )
            finally:
                self.holder = None
                self.holder_since = None

    async def _run_steps(self, steps, url, frugal, holder, mode, switched, want_snapshot):
        block = _BLOCK_STRICT if str(frugal).lower() == "strict" else (_BLOCK_NORMAL if frugal else set())
        route_handler = None
        results = []
        try:
            if block:
                async def _block(route):
                    if route.request.resource_type in block:
                        await route.abort()
                    else:
                        await route.continue_()
                await self._page.route("**/*", _block)
                route_handler = _block

            if url:
                activity.add(kind="action", mode=mode.name, holder=holder, action="goto", detail=url)
                await self._page.goto(url, wait_until="domcontentloaded")

            for i, step in enumerate(steps):
                try:
                    ceiling = max(STEP_TIMEOUT, float(step.get("timeout", 0)) / 1000 + 10)
                    value = await asyncio.wait_for(self._do(step), timeout=ceiling)
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
                    return await self._failure(exc, i, results, holder, mode, switched, want_snapshot)

            out = {"ok": True, "mode": mode.name, "results": results,
                   "holder": holder, "url": self._page.url}
            if switched:
                out["switched_mode_to"] = switched
            challenge = await snapshot.detect(self._page)
            if challenge:
                out["challenge"] = challenge
            return out
        except Exception as exc:
            return await self._failure(exc, len(results), results, holder, mode, switched, want_snapshot)
        finally:
            if route_handler:
                try:
                    await self._page.unroute("**/*", route_handler)
                except Exception:
                    pass

    async def _failure(self, exc, index, results, holder, mode, switched, want_snapshot):
        """Fail soft: everything that worked, where it broke, and what the page
        looks like now, so the caller can decide rather than guess."""
        state = {"url": self._page.url if self._page else None, "http_status": self._last_status}
        # Diagnostics must not themselves hang. We are often here *because* the
        # page is misbehaving, so everything below is on a short leash.
        try:
            state["challenge"] = await asyncio.wait_for(snapshot.detect(self._page), timeout=25)
        except Exception:
            state["challenge"] = None
        if want_snapshot:
            try:
                snap = await asyncio.wait_for(
                    snapshot.take(self._page, max_elements=200), timeout=40
                )
                self._sigs.update(snap.pop("signatures", {}))
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

    async def _locate(self, step):
        """Resolve a step's target to an ElementHandle.

        `ref` (from a snapshot) wins over `selector`. Refs fail closed (see
        snapshot.resolve) because clicking the wrong control on a real job
        application is the worst thing this service can do.
        """
        ref = step.get("ref")
        if ref:
            return await snapshot.resolve(self._page, ref, {}, self._sigs)
        sel = step.get("selector")
        if not sel:
            raise ValueError("step needs a `ref` (from a snapshot) or a `selector`")
        el = await self._page.wait_for_selector(
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

    async def _do(self, step: dict):
        """Execute one generic action. Returns a JSON-serialisable value."""
        action = step.get("action")
        p = self._page

        # --- perception ---
        if action == "snapshot":
            snap = await snapshot.take(
                p, max_elements=int(step.get("max_elements", 400)), within=step.get("within")
            )
            self._sigs.update(snap.pop("signatures", {}))
            return snap
        if action == "check_challenge":
            return {"challenge": await snapshot.detect(p), "http_status": self._last_status}

        # --- navigation ---
        if action == "goto":
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
            el = await self._locate(step)
            await el.scroll_into_view_if_needed(timeout=step.get("timeout", 15000))
            await el.click(timeout=step.get("timeout", 15000))
            return {"clicked": step.get("ref") or step.get("selector")}
        if action == "hover":
            el = await self._locate(step)
            await el.hover(timeout=step.get("timeout", 15000))
            return {"hovered": step.get("ref") or step.get("selector")}
        if action == "focus":
            el = await self._locate(step)
            await el.focus()
            return {"focused": step.get("ref") or step.get("selector")}
        if action == "fill":
            el = await self._locate(step)
            await el.fill(step["value"], timeout=step.get("timeout", 15000))
            return {"filled": step.get("ref") or step.get("selector")}
        if action == "type":
            el = await self._locate(step)
            await self._type_human(el, step["value"], step.get("delay"))
            return {"typed": step.get("ref") or step.get("selector")}
        if action == "clear":
            el = await self._locate(step)
            await el.fill("")
            return {"cleared": step.get("ref") or step.get("selector")}
        if action == "press":
            key = step["key"]
            if step.get("ref") or step.get("selector"):
                el = await self._locate(step)
                await el.press(key)
            else:
                await p.keyboard.press(key)
            return {"pressed": key}
        if action == "select_option":
            el = await self._locate(step)
            by = step.get("by", "label")
            kwargs = {by: step["value"]} if by in ("label", "value", "index") else {"label": step["value"]}
            return {"selected": await el.select_option(**kwargs)}
        if action == "scroll":
            await p.mouse.wheel(0, int(step.get("dy", 600)))
            return {"scrolled": step.get("dy", 600)}
        if action == "scroll_into_view":
            el = await self._locate(step)
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
                    el = await self._locate(step)
                    await el.click()
                chooser = await info.value
                await chooser.set_files(files)
            else:
                el = await self._locate(step)
                await el.set_input_files(files)
            return {"uploaded": [os.path.basename(f) for f in files]}
        if action == "download":
            async with p.expect_download(timeout=step.get("timeout", 30000)) as info:
                el = await self._locate(step)
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
                el = await self._locate(step)
                return {"text": await el.inner_text()}
            return {"text": await p.inner_text("body")}
        if action == "read_html":
            if step.get("ref") or step.get("selector"):
                el = await self._locate(step)
                return {"html": await el.inner_html()}
            return {"html": await p.content()}
        if action == "get_attribute":
            el = await self._locate(step)
            return {"value": await el.get_attribute(step["name"])}
        if action == "query_all":
            els = await p.query_selector_all(step["selector"])
            return {"texts": [await e.inner_text() for e in els]}
        if action == "eval":
            return {"value": await p.evaluate(step["script"])}
        if action == "screenshot":
            return await self._screenshot(step)

        raise ValueError(f"unknown action: {action!r}")

    async def _screenshot(self, step):
        """Capture the page, optionally with ref badges drawn on.

        Labels are drawn inside each frame's own document rather than composited
        in the parent. The browser then does the compositing for us, which is
        what makes it correct at any iframe nesting depth.
        """
        label = bool(step.get("label_refs"))
        p = self._page
        if label:
            snap = await snapshot.take(p, max_elements=int(step.get("max_elements", 400)))
            self._sigs.update(snap.pop("signatures", {}))
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
        name = f"{int(time.time() * 1000)}.png"
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
