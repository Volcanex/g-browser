#!/usr/bin/env python3
"""gbrowser assist: "a human is needed at this browser session".

Lives here rather than in a consumer because gbrowser owns the three things the
primitive needs: the session, the global lock, and challenge detection. A
consumer that rolled its own would be guessing at all three.

Two decisions worth knowing:

- **Quiet hours 02:00-08:00 defer, they do not wait.** assist holds a consumer
  for up to `timeout` seconds, and that consumer is usually holding a queue. An
  overnight run that stops for two minutes per wall spends the night asleep at a
  captcha nobody is awake to solve, so at night we return **8** immediately and
  let the caller skip the item and move on. Quiet hours are the owner's local
  time (`GBROWSER_ASSIST_TZ`, default `Europe/London`), NOT the host's. h runs
  on UTC, so host-local would drift the window by an hour every summer.

- **Resolution is read off the page first, acked second.** A second call
  ("tell me when you're done") is a call nobody makes at 4pm with a phone in
  their hand, so assist watches for the human's work landing in the DOM. The ack
  URL exists because the page signals are best-effort: see `resolution()`.

The mode passed in is asserted against the live browser, never applied. Applying
it would relaunch Chrome (see modes.py) and destroy the very page a human was
being summoned to.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO = Path(__file__).resolve().parent
VAR = Path(os.environ.get("GBROWSER_VAR") or _REPO / "var")
ASSIST_DIR = VAR / "assist"
PENDING_DIR = ASSIST_DIR / "pending"
LOG = ASSIST_DIR / "log.jsonl"


def _instance_env() -> Path | None:
    """The instance passport, found the way g-env's resolver finds it (copied, not
    imported, as that contract asks): GCORE_ENV, then GCORE_DIR, GSITE_GCORE_DIR or
    G_INSTANCE, then a gp-core or legacy gcore checkout beside this one."""
    if os.environ.get("GCORE_ENV"):
        return Path(os.environ["GCORE_ENV"])
    for v in ("GCORE_DIR", "GSITE_GCORE_DIR", "G_INSTANCE"):
        if os.environ.get(v):
            return Path(os.environ[v]) / ".env"
    return next((_REPO.parent / n / ".env" for n in ("gp-core", "gcore")
                 if (_REPO.parent / n).is_dir()), None)


# The g-brain checkout is on this list for one key: NTFY_URL lives with the
# spine. This reads that file, it does not import g-brain. Copying the URL into
# a second .env would silently drift the day the topic is rotated.
_BRAIN_ENV = next((_REPO.parent / n / ".env" for n in ("g-brain", "brain-tools")
                   if (_REPO.parent / n).is_dir()), None)
# Same defensive resolution as the alert spine: services here are launched from
# different units and do not all load the same EnvironmentFile. Read in-process,
# never echo. Anchored on this file, not $HOME.
ENV_FILES = [f for f in (_instance_env(), _REPO / ".env", _BRAIN_ENV) if f]

QUIET_START, QUIET_END = 2, 8
TZ = ZoneInfo(os.environ.get("GBROWSER_ASSIST_TZ", "Europe/London"))
POLL_S = float(os.environ.get("GBROWSER_ASSIST_POLL", "5"))

EXIT_OK, EXIT_FAIL, EXIT_TIMEOUT, EXIT_DEFERRED = 0, 1, 7, 8

_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")

# The human's work, as it lands in the page. Turnstile/reCAPTCHA/hCaptcha all
# write their solved token into a hidden field in the *host* document, which is
# the one contract these widgets publish; their internal DOM is not one.
PROBE_JS = """
() => {
  const NAMES = ['cf-turnstile-response', 'g-recaptcha-response', 'h-captcha-response'];
  const found = new Set();
  const scanRoot = (root, depth) => {
    if (!root || !root.querySelectorAll || depth > 4) return;
    for (const n of NAMES)
      for (const el of root.querySelectorAll('[name="' + n + '"]'))
        if (el.value) found.add(n);
    for (const el of root.querySelectorAll('*'))
      if (el.shadowRoot) scanRoot(el.shadowRoot, depth + 1);
  };
  const scanWindow = (w, depth) => {
    if (depth > 3) return;
    let doc = null;
    try { doc = w.document; } catch (e) { return; }
    scanRoot(doc, 0);
    for (let i = 0; i < w.frames.length; i++) {
      try { scanWindow(w.frames[i], depth + 1); } catch (e) {}
    }
  };
  scanWindow(window, 0);
  return { url: location.href, tokens: [...found] };
}
"""


def env(key: str) -> str:
    """Resolve a secret: live environment first, then the known .env files.
    Read in-process only: never printed, never logged."""
    v = os.environ.get(key)
    if v:
        return v
    for f in ENV_FILES:
        try:
            for line in f.read_text().splitlines():
                line = line.strip()
                if line.startswith(key + "="):
                    return line[len(key) + 1:].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def in_quiet_hours(now: dt.datetime) -> bool:
    """02:00-08:00 in the owner's timezone. 08:00 sharp is awake."""
    local = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    return QUIET_START <= local.hour < QUIET_END


def resolution(baseline: dict, probe: dict):
    """What (if anything) says a human has dealt with it. Pure.

    Ordered by how much each signal can be trusted:

    1. `ack`: someone said so. Unambiguous.
    2. `challenge cleared`: the wall the snapshot saw is gone. Catches every
       full-page interstitial, but NOT an embedded checkbox: `detect()` in
       snapshot.py has no rule for a Turnstile widget sitting in a form, so
       there is nothing to clear.
    3. `token`: a captcha response field that was empty now holds a value.
       This is the embedded-widget case. Best-effort: `eval` runs in the main
       document, so a form inside a cross-origin ATS iframe is out of reach.
    4. `navigated`: the URL moved. Someone did something.

    Anything a signal misses falls through to the ack URL in the push, which is
    why the push always carries one.
    """
    if probe.get("acked"):
        return "ack"

    was = (baseline.get("challenge") or {}).get("confidence")
    now = (probe.get("challenge") or {}).get("confidence")
    if was in ("high", "medium") and now not in ("high", "medium"):
        return "challenge cleared"

    new_tokens = sorted(set(probe.get("tokens") or []) - set(baseline.get("tokens") or []))
    if new_tokens:
        return f"token: {', '.join(new_tokens)}"

    if baseline.get("url") and probe.get("url") and baseline["url"] != probe["url"]:
        return "navigated"

    return None


# ---- pending requests (the ack side) --------------------------------------

def open_pending(reason: str, url: str, mode: str) -> str:
    """Register a waiting request and return its nonce.

    The nonce IS the authorisation on the ack route: unguessable, single
    request, dead the moment the wait ends. All it can do is stop a wait early,
    which is why it is safe to put in a push notification, unlike the service
    token, which must never leave this host.
    """
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    nonce = secrets.token_hex(16)
    (PENDING_DIR / f"{nonce}.json").write_text(json.dumps({
        "reason": reason, "url": url, "mode": mode, "opened": time.time(),
    }))
    return nonce


def ack(nonce: str) -> bool:
    """Mark a pending request resolved. Returns False for anything unknown:
    an expired or invented nonce must not look like a success."""
    if not _NONCE_RE.match(nonce or ""):
        return False
    if not (PENDING_DIR / f"{nonce}.json").is_file():
        return False
    (PENDING_DIR / f"{nonce}.ack").write_text("")
    return True


def is_acked(nonce: str) -> bool:
    return (PENDING_DIR / f"{nonce}.ack").is_file()


def close_pending(nonce: str) -> None:
    for suffix in (".json", ".ack"):
        try:
            (PENDING_DIR / f"{nonce}{suffix}").unlink()
        except OSError:
            pass


# ---- the push -------------------------------------------------------------

def _ascii(s: str, limit: int = 200) -> str:
    """Header-safe: urllib encodes headers as latin-1, and a newline in one is a
    header-injection hole. Callers' reasons are arbitrary text."""
    return re.sub(r"[^\x20-\x7e]", " ", (s or ""))[:limit]


def push(reason: str, url: str, mode: str, nonce: str) -> dict:
    """One ntfy push, tappable.

    Deliberately not `brain_alerts.send()`: importing brain-tools would couple
    the browser to the knowledge base for a URL, and the stock send() has no
    Click/Actions, which is the whole point here. A notification you cannot act
    on from the lock screen is a notification that waits for a laptop.
    """
    ntfy = env("NTFY_URL")
    if not ntfy:
        return {"ok": False, "error": "NTFY_URL unset"}
    # The panel's public URL is per instance, so there is no default. Without it
    # the push still goes out, because a human who knows is better than a dead
    # wait, but it carries no links and the page signals are the only way back.
    panel = env("GBROWSER_PANEL_URL").rstrip("/")
    body = f"{reason}\n{url or 'no page'}\nmode: {mode}"
    headers = {
        "Title": "browser needs a human",
        "Priority": "high",
        "Tags": "warning",
    }
    if panel:
        headers["Click"] = panel
        headers["Actions"] = (f"http, Done, {panel}/api/assist/ack/{nonce}, method=POST, clear=true; "
                              f"view, Open browser, {panel}")
    req = urllib.request.Request(ntfy, data=body.encode("utf-8"),
                                 headers={k: _ascii(v, 400) for k, v in headers.items()})
    try:
        urllib.request.urlopen(req, timeout=10)
        if not panel:
            return {"ok": True, "warning": "GBROWSER_PANEL_URL unset, so the push has no links"}
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---- the record -----------------------------------------------------------

def log(**row) -> None:
    """One JSONL line per request. This is the honest record of how often the
    automation could not finish on its own, the number that says whether a flow
    is worth keeping."""
    row["ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        ASSIST_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


# ---- the wait ------------------------------------------------------------

def probe(post, mode: str) -> dict:
    """One read of the page: what wall is on it, what tokens are filled, where
    it is. Two steps in one job so it costs one lock acquisition."""
    result = post("/run", {"mode": mode, "timeout_s": 30, "snapshot_on_error": False, "steps": [
        {"action": "check_challenge"},
        {"action": "eval", "script": PROBE_JS, "optional": True},
    ]}, consumer="assist", timeout=60)
    out = {"challenge": None, "tokens": [], "url": None}
    for value in result.get("results") or []:
        if not isinstance(value, dict):
            continue
        if "challenge" in value:
            out["challenge"] = value["challenge"]
        inner = value.get("value")
        if isinstance(inner, dict):
            out["tokens"] = inner.get("tokens") or []
            out["url"] = inner.get("url")
    return out


def request(reason, mode, timeout, get, post, out, now=None,
            sleep=time.sleep, clock=time.monotonic):
    """Ask for a human and wait. Returns an exit code.

    `get`/`post`/`out`/`sleep`/`clock` are injected so the whole flow is
    exercisable without a browser, a network, or a two-minute test.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    if in_quiet_hours(now):
        log(outcome="deferred", reason=reason, mode=mode, url=None, waited_s=0,
            signal=None, local_hour=now.astimezone(TZ).hour)
        out(f"assist deferred: quiet hours ({QUIET_START:02d}:00-{QUIET_END:02d}:00), "
            f"not pinging, not waiting. Skip this one and carry on.")
        return EXIT_DEFERRED

    status = (get("/health") or {}).get("status") or {}
    live_mode, url = status.get("mode"), status.get("current_url")
    if live_mode != mode:
        out(f"assist refused: browser is in {live_mode or 'no'} mode, not {mode}. "
            f"Running the probe would relaunch Chrome and lose the page a human is needed for.")
        log(outcome="refused", reason=reason, mode=mode, url=url, waited_s=0,
            signal=f"mode mismatch ({live_mode})")
        return EXIT_FAIL

    baseline = probe(post, mode)
    url = baseline.get("url") or url
    nonce = open_pending(reason, url, mode)
    started = clock()
    try:
        sent = push(reason, url, mode, nonce)
        if not sent.get("ok"):
            # A wait nobody was told about is a dead wait; fail fast instead.
            out(f"assist could not notify ({sent.get('error')}), not waiting.")
            log(outcome="unnotified", reason=reason, mode=mode, url=url, waited_s=0,
                signal=sent.get("error"))
            return EXIT_FAIL

        if sent.get("warning"):
            out(f"assist: {sent['warning']}.")
        out(f"assist: pushed ({reason}). Waiting up to {timeout:.0f}s for a human.")
        while clock() - started < timeout:
            sleep(min(POLL_S, max(0.0, timeout - (clock() - started))))
            current = probe(post, mode)
            current["acked"] = is_acked(nonce)
            signal = resolution(baseline, current)
            if signal:
                waited = clock() - started
                out(f"assist: resolved after {waited:.0f}s ({signal}).")
                log(outcome="resolved", reason=reason, mode=mode, url=url,
                    waited_s=round(waited, 1), signal=signal)
                return EXIT_OK

        waited = clock() - started
        out(f"assist: no human after {waited:.0f}s.")
        log(outcome="timeout", reason=reason, mode=mode, url=url,
            waited_s=round(waited, 1), signal=None)
        return EXIT_TIMEOUT
    finally:
        close_pending(nonce)
