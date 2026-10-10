#!/usr/bin/env python3
"""Phase 1 smoke test: perception, modes, and failing safely.

Exercises the live service over HTTP, the same way a consumer would. Run with
the service up:

    .venv/bin/python tests/smoke_phase1.py

The challenge cases inject each wall's signature into a blank page rather than
hitting a real bot wall: real walls are non-deterministic (our stealth stack
often passes Cloudflare outright, which is the point of it) and hammering them
to fail a test is exactly the behaviour the governor will exist to prevent.
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("GBROWSER_URL", "http://127.0.0.1:8098")
TOKEN = os.environ.get("GBROWSER_SERVICE_TOKEN", "")

passed = failed = 0


def run(job, expect_ok=True):
    req = urllib.request.Request(
        f"{BASE}/run",
        data=json.dumps(job).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Gbrowser-Token": TOKEN,
            "X-Gbrowser-Consumer": "smoke-phase1",
        },
    )
    with urllib.request.urlopen(req, timeout=200) as r:
        return json.load(r)


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def blank(script):
    """A blank page with `script` applied, then a challenge check."""
    return run({
        "mode": "research",
        "url": "about:blank",
        "steps": [
            {"action": "eval", "script": f"(()=>{{{script};return 1}})()"},
            {"action": "check_challenge"},
        ],
    })


print("modes")
r = run({"url": "about:blank", "steps": [{"action": "wait", "ms": 10}]})
check("a job with no mode is refused", r.get("ok") is False and "no `mode`" in r.get("error", ""),
      r.get("error", ""))
r = run({"mode": "nonsense", "steps": [{"action": "wait", "ms": 10}]})
check("an unknown mode is refused", r.get("ok") is False and "unknown mode" in r.get("error", ""),
      r.get("error", ""))

print("challenge detection")
CASES = [
    ("cloudflare", 'document.title="Just a moment...";'
                   'document.body.innerHTML="<div id=challenge-running></div>"', "cloudflare", "high"),
    ("datadome", 'document.body.innerHTML='
                 '"<iframe src=\\"https://geo.captcha-delivery.com/captcha/\\"></iframe>"',
     "datadome", "high"),
    ("hcaptcha", 'document.body.innerHTML="<div class=h-captcha></div>"', "hcaptcha", "high"),
    ("login wall", 'document.body.innerHTML="<p>Sign in to view this profile</p>"', "login", "high"),
    ("generic block", 'document.body.innerHTML='
                      '"<h1>Access Denied</h1><p>unusual traffic from your network</p>"',
     "generic", "medium"),
]
for name, js, kind, confidence in CASES:
    r = blank(js)
    got = r["results"][-1]["challenge"] if r.get("ok") else None
    check(f"{name} detected as {kind}/{confidence}",
          bool(got) and got["kind"] == kind and got["confidence"] == confidence,
          json.dumps(got))

r = blank('document.title="Hello";document.body.innerHTML="<h1>An ordinary page</h1>"')
check("an ordinary page reports no challenge",
      r.get("ok") and r["results"][-1]["challenge"] is None,
      json.dumps(r.get("results", [])[-1:]))

print("snapshot")
r = run({
    "mode": "research",
    "url": "about:blank",
    "steps": [
        {"action": "eval", "script": '(()=>{document.body.innerHTML='
                                     '"<h1>Apply</h1>'
                                     '<label for=e>Email</label><input id=e type=email required>'
                                     '<input id=cv type=file>'
                                     '<select id=s><option>Yes</option><option>No</option></select>'
                                     '<button>Submit application</button>";return 1})()'},
        {"action": "snapshot"},
    ],
})
snap = r["results"][-1]["snapshot"] if r.get("ok") else ""
check("labels the email field and its required state",
      'textbox   "Email"' in snap and "required" in snap, snap[:200])
check("finds the file input", "file " in snap and "no file" in snap, snap[:200])
check("counts select options", "2 options" in snap, snap[:200])
check("finds the submit button", '"Submit application"' in snap, snap[:200])
check("no signatures leak to the consumer", "signatures" not in r["results"][-1])

print("refs fail closed")
r = run({"mode": "research", "steps": [{"action": "click", "ref": "e99999"}]})
check("an unknown ref is refused, not guessed",
      r.get("ok") is False and "ref map" in r.get("error", ""), r.get("error", ""))
check("a failure carries a snapshot of where it stopped",
      "snapshot" in r.get("state", {}), json.dumps(r.get("state", {}))[:120])
r = run({"mode": "research", "steps": [{"action": "click", "ref": "not-a-ref"}]})
check("a malformed ref is refused", r.get("ok") is False and "malformed ref" in r.get("error", ""),
      r.get("error", ""))

print("failing soft")
r = run({
    "mode": "research",
    "snapshot_on_error": False,
    "steps": [
        {"action": "eval", "script": "1+1"},
        {"action": "click", "selector": "#missing", "optional": True, "timeout": 1500},
        {"action": "click", "selector": "#also-missing", "timeout": 1500},
    ],
})
check("keeps results from before the failure", len(r.get("results", [])) == 2,
      json.dumps(r.get("results"))[:160])
check("names the step that failed", r.get("failed_step") == 2, str(r.get("failed_step")))
check("an optional step is skipped, not fatal",
      isinstance(r.get("results", [None, None])[1], dict) and r["results"][1].get("skipped") is True)

print("uploads are confined")
r = run({"mode": "research", "steps": [
    {"action": "upload", "selector": "#cv", "files": [".env"]}]})
check("refuses to upload a file outside the upload dir",
      r.get("ok") is False and "upload refused" in r.get("error", ""), r.get("error", ""))

print("job budget")
r = run({"mode": "research", "timeout_s": 5,
         "steps": [{"action": "wait", "ms": 30000}]})
check("a job that overruns its budget fails instead of wedging the browser",
      r.get("ok") is False and ("budget" in r.get("error", "") or "exceeded" in r.get("error", "")),
      r.get("error", ""))

with urllib.request.urlopen(f"{BASE}/health", timeout=10) as h:
    st = json.load(h)["status"]
check("the lock is free afterwards", st["busy"] is False, json.dumps(st)[:120])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
