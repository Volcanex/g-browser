#!/usr/bin/env python3
"""gbrowser: drive the shared g-browser service from a shell.

This exists instead of an MCP server. gbrowser's consumer is an agent running on
the same host, so the OAuth and tunnel cost that brain pays for its MCP face buys
nothing here; a CLI gives the same typed ergonomics for a fraction of the work,
and `POST /run` stays the seam if MCP is ever actually wanted.

Everything is a thin composition of the job API. No browser logic lives here.

    gbrowser snapshot --mode research --url https://example.com/jobs
    gbrowser click    --mode research --ref e24
    gbrowser upload   --mode identity --ref e24 --chooser --file document.pdf

Two deliberate frictions:

- `--mode` is required for anything that touches a page, mirroring the
  no-default rule in browser/modes.py. A CLI that picked a mode for you would
  reintroduce exactly the failure that rule exists to prevent: a caller who
  forgot to think about identity silently borrowing the owner's.
- A detected bot wall exits **3**, not 1, so a shell loop can tell "this failed"
  from "we have been walled and should stop". Retrying into a challenge is how
  IPs get burned.

`assist` is the one command with logic of its own; it lives in assist.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import assist
from browser.fallback import EXIT_BUDGET

BASE = os.environ.get("GBROWSER_URL", "http://127.0.0.1:8098")

EXIT_OK, EXIT_FAIL, EXIT_CHALLENGE = 0, 1, 3
EXIT_TIMEOUT, EXIT_DEFERRED = assist.EXIT_TIMEOUT, assist.EXIT_DEFERRED

# Modes that touch a page must be named explicitly; see the module docstring.
PAGE_COMMANDS = {"snapshot", "click", "fill", "type", "select", "press", "scroll",
                 "upload", "download", "screenshot", "goto", "read", "challenge", "run"}


def token() -> str:
    """Service token from the environment, else the passport. Never printed."""
    t = assist.env("GBROWSER_SERVICE_TOKEN")
    if not t:
        sys.exit("no GBROWSER_SERVICE_TOKEN in env or passport")
    return t


def post(path: str, payload: dict, consumer: str, timeout=300):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "X-Gbrowser-Token": token(),
                 "X-Gbrowser-Consumer": consumer})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 429:
            try:
                result = json.loads(body)
                if result.get("exit_code") == EXIT_BUDGET:
                    return result
            except (ValueError, AttributeError):
                pass
        sys.exit(f"gbrowser {e.code}: {body[:400]}")
    except urllib.error.URLError as e:
        sys.exit(f"gbrowser unreachable at {BASE}: {e.reason}")


def get(path: str, timeout=60):
    req = urllib.request.Request(f"{BASE}{path}", headers={"X-Gbrowser-Token": token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"gbrowser {e.code}: {e.read(300).decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"gbrowser unreachable at {BASE}: {e.reason}")


def target(args) -> dict:
    """A ref or a selector, never both, at least one where required."""
    if getattr(args, "ref", None):
        return {"ref": args.ref}
    if getattr(args, "selector", None):
        return {"selector": args.selector}
    return {}


def render(result: dict, args) -> int:
    """Human output by default. The snapshot outline is already designed to be
    read, so it is printed verbatim rather than wrapped in JSON."""
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        _render_human(result, args)

    if result.get("exit_code") == EXIT_BUDGET:
        return EXIT_BUDGET
    challenge = result.get("challenge") or (result.get("state") or {}).get("challenge")
    if challenge and challenge.get("confidence") in ("high", "medium"):
        return EXIT_CHALLENGE
    return EXIT_OK if result.get("ok") else EXIT_FAIL


def _render_human(result: dict, args):
    if not result.get("ok"):
        print(f"FAILED at step {result.get('failed_step')}: {result.get('error')}", file=sys.stderr)
        state = result.get("state") or {}
        if state.get("http_status"):
            print(f"  http {state['http_status']}  url {state.get('url')}", file=sys.stderr)
        if state.get("snapshot"):
            print("\n--- page as it stands ---", file=sys.stderr)
            print(state["snapshot"][:4000], file=sys.stderr)

    for value in result.get("results", []):
        if not isinstance(value, dict):
            print(value)
            continue
        if "snapshot" in value:
            print(value["snapshot"])
            if value.get("truncated"):
                print("\n[truncated: raise --max or scope with --within]")
        elif "text" in value:
            print(value["text"])
        elif "html" in value:
            print(value["html"])
        elif "png_b64" in value:
            print(value.get("url"))
        elif value.get("skipped"):
            print(f"[skipped: {value.get('error', '')[:120]}]", file=sys.stderr)
        else:
            print(json.dumps(value))

    challenge = result.get("challenge") or (result.get("state") or {}).get("challenge")
    if challenge:
        print(f"\n[challenge: {challenge['kind']} ({challenge['confidence']}): "
              f"{challenge['evidence']}]", file=sys.stderr)


def build_job(args) -> dict:
    cmd = args.cmd
    job: dict = {"mode": args.mode}
    if getattr(args, "fallback", False):
        job["fallback"] = True
    if getattr(args, "frugal", None) is not None:
        job["frugal"] = args.frugal
    if getattr(args, "timeout", None):
        job["timeout_s"] = args.timeout
    if getattr(args, "url", None) and cmd != "goto":
        job["url"] = args.url

    if cmd == "run":
        raw = sys.stdin.read() if args.steps == "-" else open(args.steps).read()
        steps = json.loads(raw)
        job["steps"] = steps if isinstance(steps, list) else [steps]
        return job

    step: dict = {}
    if cmd == "snapshot":
        step = {"action": "snapshot", "max_elements": args.max}
        if args.within:
            step["within"] = args.within
    elif cmd == "goto":
        step = {"action": "goto", "url": args.url}
    elif cmd == "click":
        step = {"action": "click", **target(args)}
    elif cmd == "fill":
        step = {"action": "fill", "value": args.value, **target(args)}
    elif cmd == "type":
        step = {"action": "type", "value": args.value, **target(args)}
    elif cmd == "select":
        step = {"action": "select_option", "value": args.value, **target(args)}
    elif cmd == "press":
        step = {"action": "press", "key": args.key, **target(args)}
    elif cmd == "scroll":
        step = {"action": "scroll", "dy": args.dy}
    elif cmd == "read":
        step = {"action": "read_text", **target(args)}
    elif cmd == "challenge":
        step = {"action": "check_challenge"}
    elif cmd == "upload":
        step = {"action": "upload", "files": args.file, **target(args)}
        if args.chooser:
            step["via"] = "chooser"
    elif cmd == "download":
        step = {"action": "download", **target(args)}
    elif cmd == "screenshot":
        step = {"action": "screenshot", "label_refs": args.labels,
                "full_page": args.full, "inline": args.inline}
    else:
        sys.exit(f"unknown command {cmd}")

    # Acting on a page and then seeing the result is the normal loop, so
    # --then-snapshot saves a whole round trip and a second lock acquisition.
    job["steps"] = [step]
    if getattr(args, "then_snapshot", False) and cmd != "snapshot":
        job["steps"].append({"action": "snapshot", "max_elements": args.max})
    return job


FETCH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def do_fetch(args):
    """GET a URL through a mode's proxy and print the body as JSON.

    Reports the exit IP alongside the body because the caller usually wants to
    know *which* of the hundred datacenter IPs served a given response. A page
    that reads fine from one and returns an auth wall from another is a fact
    about the pool, not about the site, and that is only visible if it is said.
    """
    import urllib.error
    import urllib.request
    from browser import modes as _modes, proxy as _proxy

    kind = _modes.resolve(args.mode).proxy_kind
    if args.rotate and kind == _proxy.STATIC:
        print(json.dumps({"error": "refusing to rotate identity's egress: its "
                                   "cf_clearance and datadome cookies are IP-bound, "
                                   "so a profile that teleports self-flags"}), file=sys.stderr)
        return EXIT_FAIL
    p = _proxy._resolve(kind, rotate_now=args.rotate) if kind else None

    opener = urllib.request.build_opener(*( [urllib.request.ProxyHandler({
        "http": f"http://{p['username']}:{p['password']}@{p['server'].split('//',1)[1]}",
        "https": f"http://{p['username']}:{p['password']}@{p['server'].split('//',1)[1]}",
    })] if p else [urllib.request.ProxyHandler({})] ))
    req = urllib.request.Request(args.url, headers={
        "User-Agent": FETCH_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    exit_ip = p["server"].split("//", 1)[1].split(":")[0] if p else None
    try:
        r = opener.open(req, timeout=args.timeout)
        body = r.read(4_000_000).decode("utf-8", "ignore")
        status = r.status
    except urllib.error.HTTPError as e:
        body, status = e.read(200_000).decode("utf-8", "ignore"), e.code
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}", "exit_ip": exit_ip}),
              file=sys.stderr)
        return EXIT_FAIL
    print(json.dumps({"url": args.url, "http_status": status, "exit_ip": exit_ip,
                      "bytes": len(body), "body": body}))
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gbrowser", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="raw JSON instead of human output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def page(name, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--mode", required=True, choices=["research", "identity", "direct"],
                       help="research = throwaway profile + datacenter IP; "
                            "identity = your real profile + residential IP")
        p.add_argument("--url", help="navigate here first")
        p.add_argument("--timeout", type=float, help="job budget in seconds")
        p.add_argument("--frugal", choices=["true", "false", "strict"], default=None)
        p.add_argument("--fallback", action="store_true",
                       help="throttled fallback for walls g-scraper cannot pass")
        p.add_argument("--max", type=int, default=400, help="snapshot element budget")
        p.add_argument("--then-snapshot", action="store_true",
                       help="snapshot after acting, in the same job")
        return p

    def targeted(p):
        p.add_argument("--ref", help="ref from a snapshot, e.g. e24 or f1e3")
        p.add_argument("--selector", help="CSS selector (legacy path)")
        return p

    s = page("snapshot", "numbered outline of the page")
    s.add_argument("--within", help="CSS selector to scope the snapshot to")

    g = page("goto", "navigate")
    targeted(page("click", "click a ref or selector"))
    f = targeted(page("fill", "set a field's value at once"))
    f.add_argument("--value", required=True)
    t = targeted(page("type", "type with human-ish key timing"))
    t.add_argument("--value", required=True)
    se = targeted(page("select", "choose an option in a select"))
    se.add_argument("--value", required=True)
    pr = targeted(page("press", "press a key"))
    pr.add_argument("--key", required=True)
    sc = page("scroll", "scroll the page")
    sc.add_argument("--dy", type=int, default=600)
    targeted(page("read", "read text from a ref, selector, or the whole body"))
    page("challenge", "check for a bot wall")
    u = targeted(page("upload", "attach files"))
    u.add_argument("--file", required=True, action="append",
                   help="path under var/uploads (repeatable)")
    u.add_argument("--chooser", action="store_true",
                   help="click the target and answer the file chooser, needed when "
                        "the real <input type=file> is hidden behind a styled button")
    targeted(page("download", "click and save the resulting download"))
    ss = page("screenshot", "capture the page")
    ss.add_argument("--labels", action="store_true", help="draw ref badges onto the image")
    ss.add_argument("--full", action="store_true", help="full page rather than viewport")
    ss.add_argument("--inline", action="store_true", help="also return base64 (expensive)")
    r = page("run", "run a JSON step list")
    r.add_argument("--steps", default="-", help="file, or - for stdin")

    a = sub.add_parser("assist", help="ask a human to come to this browser session")
    a.add_argument("--mode", required=True, choices=["research", "identity", "direct"],
                   help="the mode the session is already in; a mismatch is refused "
                        "rather than switched, because switching relaunches Chrome")
    a.add_argument("--reason", required=True, help="what the human is being asked to do")
    a.add_argument("--timeout", type=float, default=120, help="seconds to wait (default 120)")

    fe = sub.add_parser("fetch", help="plain HTTP GET through a mode's egress, "
                                      "no browser, no lock, no rendering")
    fe.add_argument("--mode", required=True, choices=["research", "identity", "direct"],
                    help="whose egress to leave from; research is the datacenter pool")
    fe.add_argument("--url", required=True)
    fe.add_argument("--timeout", type=float, default=30)
    fe.add_argument("--rotate", action="store_true",
                    help="advance to the next IP in the pool first (research only, "
                         "refused for identity, whose cookies are IP-bound)")

    sub.add_parser("status", help="browser state, mode, lock, bandwidth")
    e = sub.add_parser("egress", help="probe a mode's proxy and report the exit IP")
    e.add_argument("--mode", choices=["research", "identity", "direct"])
    sub.add_parser("modes", help="list modes")

    args = ap.parse_args(argv)

    if getattr(args, "mode", None) == "research" and not getattr(args, "fallback", False):
        print("research mode is retired for agents: use g-scraper; --fallback for the hardest walls",
              file=sys.stderr)

    if args.cmd == "status":
        st = get("/health")["status"]
        if args.json:
            print(json.dumps(st, indent=1))
        else:
            print(f"mode      {st['mode']}  (acts as you: {st['identity']})")
            print(f"egress    {st['proxy']}")
            print(f"attached  {st['attached']}   busy: {st['busy']}"
                  f"{' by ' + str(st['holder']) if st['holder'] else ''}")
            print(f"url       {st['current_url']}")
            for mode, b in (st.get("bandwidth") or {}).items():
                print(f"bytes     {mode}: {b['total'] / 1048576:.1f} MB")
            if st.get("last_job"):
                job = st["last_job"]
                print(f"last job  {job['mode']} by {job['holder']}: {job['bytes']} bytes (estimate)")
            if st.get("fallback"):
                budget = st["fallback"]
                print(f"fallback  {budget['used']}/{budget['limit']} jobs in the rolling hour")
                for job in budget["jobs"]:
                    print(f"  job {job['id']}  {job['mode']} by {job['holder']}: {job['bytes']} bytes (estimate)")
        return EXIT_OK

    if args.cmd == "assist":
        # 0 resolved, 7 nobody came, 8 deferred to daylight.
        return assist.request(args.reason, args.mode, args.timeout, get, post,
                              out=lambda m: print(m, file=sys.stderr))

    if args.cmd == "modes":
        print(json.dumps(get("/api/modes"), indent=1))
        return EXIT_OK

    if args.cmd == "fetch":
        # Deliberately not a /run job: no Chrome, no lock, no turn-taking. Most
        # of what this browser was doing on public job pages was rendering a
        # 2MB SPA to read text that the plain HTML already carries, and doing it
        # through the one metered egress that wears the owner's face. A GET costs
        # none of that and several can run at once.
        return do_fetch(args)

    if args.cmd == "egress":
        # Admin-gated on purpose: it is a live probe, not a status read.
        q = f"?mode={args.mode}" if args.mode else ""
        print(json.dumps(get(f"/api/egress{q}"), indent=1))
        return EXIT_OK

    if args.frugal is not None:
        args.frugal = {"true": True, "false": False, "strict": "strict"}[args.frugal]

    result = post("/run", build_job(args), consumer=os.environ.get("GBROWSER_CONSUMER", "cli"))
    return render(result, args)


if __name__ == "__main__":
    sys.exit(main())
