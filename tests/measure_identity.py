"""Measure read-only identity loads through the live service, without bodies/cookies.

Usage: .venv/bin/python tests/measure_identity.py before|after
CDP observes transferred bytes; the service alone owns navigation and the profile.
"""
import asyncio
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
import websockets

PAGES = [
    "https://www.linkedin.com/jobs/",
    "https://www.indeed.co.uk/",
    "https://www.vinted.co.uk/items/9057231353-next-dress",
]


class Observer:
    def __init__(self, ws):
        self.ws = ws
        self.pending = {}
        self.handlers = {}
        self.serial = 0

    def on(self, method, handler):
        self.handlers[method] = handler

    async def read(self):
        async for raw in self.ws:
            message = json.loads(raw)
            if "id" in message:
                future = self.pending.pop(message["id"], None)
                if future is not None and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError(str(message["error"])))
                    else:
                        future.set_result(message.get("result", {}))
            elif message.get("method") in self.handlers:
                self.handlers[message["method"]](message["params"])

    async def send(self, method):
        self.serial += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.serial] = future
        await self.ws.send(json.dumps({"id": self.serial, "method": method, "params": {}}))
        return await asyncio.wait_for(future, 10)


def targets():
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5) as response:
        return json.load(response)


async def main():
    rows = []
    marker = "about:blank#gbrowser-bandwidth-measurement"
    initial = await asyncio.to_thread(cli.post, "/run", {
        "mode": "identity", "url": marker, "steps": [{"action": "wait", "ms": 10}],
    }, "bandwidth-measurement")
    assert initial["ok"], initial
    pages = await asyncio.to_thread(targets)
    target = next(p for p in pages if p["type"] == "page" and p["url"] == marker)
    # A second Playwright/Patchright client changes target interception/auth state.
    # Raw CDP observes only Network and leaves the owning client untouched.
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=16_000_000) as ws:
        cdp = Observer(ws)
        reader = asyncio.create_task(cdp.read())
        await cdp.send("Network.enable")
        # Equal cold-cache starting conditions; leave cookies and logins intact.
        await cdp.send("Network.clearBrowserCache")
        requests = {}
        totals = Counter()
        hosts = Counter()
        cache_hits = []

        def request(ev):
            requests[ev["requestId"]] = (_host(ev["request"]["url"]), ev.get("type", "Other"))
            redirect = ev.get("redirectResponse")
            if redirect:
                n = int(redirect.get("encodedDataLength", 0))
                totals["Document"] += n
                hosts[_host(redirect["url"])] += n

        def response(ev):
            if ev["response"].get("fromDiskCache") or ev["response"].get("fromServiceWorker"):
                cache_hits.append(ev["requestId"])

        def finished(ev):
            host, kind = requests.get(ev["requestId"], ("?", "Other"))
            n = int(ev.get("encodedDataLength", 0))
            if ev["requestId"] in cache_hits:
                n = 0
            totals[kind] += n
            hosts[host] += n

        cdp.on("Network.requestWillBeSent", request)
        cdp.on("Network.responseReceived", response)
        cdp.on("Network.loadingFinished", finished)
        for url in PAGES:
            requests.clear()
            totals.clear()
            hosts.clear()
            cache_hits.clear()
            result = await asyncio.to_thread(cli.post, "/run", {
                "mode": "identity", "url": url, "timeout_s": 55, "params": {"tab_id": initial["tab_id"]},
                "steps": [{"action": "wait", "ms": 8000}, {"action": "check_challenge"}],
            }, "bandwidth-measurement")
            roots = {_host(url).removeprefix("www.")}
            if "indeed" in url:
                roots.add("indeed.com")
            first = sum(n for host, n in hosts.items()
                        if any(host == root or host.endswith("." + root) for root in roots))
            row = {"requested_url": url, "url": result.get("url"), "ok": result["ok"],
                   "challenge": result.get("challenge"), "error": result.get("error"),
                   "bytes": sum(totals.values()), "by_resource_type": dict(totals),
                   "by_host": dict(hosts.most_common()), "first_party": first,
                   "third_party": sum(hosts.values()) - first, "cache_hits": len(cache_hits)}
            rows.append(row)
            print(json.dumps(row), flush=True)
            # Stop the page's background traffic between samples.
            await asyncio.to_thread(cli.post, "/run", {
                "mode": "identity", "url": "about:blank", "params": {"tab_id": result.get("tab_id")},
                "steps": [{"action": "wait", "ms": 10}],
            }, "bandwidth-measurement")
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    dest = Path(__file__).resolve().parent / ("identity_" + sys.argv[1] + ".json")
    dest.write_text(json.dumps(rows, indent=2) + "\n")
    if not all(r["ok"] for r in rows):
        raise SystemExit("measurement contains failed navigation; do not interpret as savings")


def _host(url):
    return urlparse(url).hostname or "?"


if __name__ == "__main__":
    asyncio.run(main())
