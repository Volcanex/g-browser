"""Check actual disk-cache reuse and identity image opt-in in an isolated profile."""
import asyncio
import base64
import os
import sys
import tempfile
import threading
from collections import Counter
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser import core, modes

hits = Counter()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        if self.path.startswith("http:"):
            expected = "Basic " + base64.b64encode(b"cache-test:test-only").decode()
            if self.headers.get("Proxy-Authorization") != expected:
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="cache-test"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.path = urlparse(self.path).path
        hits[self.path] += 1
        if self.path == "/asset.js":
            body = b'document.documentElement.dataset.cacheMarker="yes";/*' + b"x" * 20000 + b"*/"
            mime = "application/javascript"
        elif self.path == "/image.png":
            body = b"image"
            mime = "image/png"
        else:
            body = b'<script src="/asset.js"></script><img src="/image.png"><h1>Cache fixture</h1>'
            mime = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400" if self.path != "/" else "no-cache")
        self.end_headers()
        self.wfile.write(body)


async def main(url, profile):
    modes.MODES["direct"] = replace(modes.DIRECT, profile=profile, identity=True)
    core.CHROME_ARGS = [a for a in core.CHROME_ARGS if not a.startswith("--remote-debugging-port=")]
    core.CHROME_ARGS.append("--remote-debugging-port=0")
    manager = core.BrowserManager()
    try:
        await manager.start("direct")
        assert manager.attached, manager.last_error
        job = {"mode": "direct", "url": url, "steps": [
            {"action": "wait", "ms": 700}, {"action": "eval", "script": "document.documentElement.dataset.cacheMarker"}]}
        first = await manager.run(job, "cache-test")
        assert first["ok"] and first["results"][-1]["value"] == "yes", first
        assert hits["/image.png"] == 0, hits
        second = await manager.run({**job, "images": True, "tab_id": first["tab_id"]}, "cache-test")
        assert second["ok"] and hits["/image.png"] == 1, (second, hits)
        assert hits["/asset.js"] == 1, hits
        await manager.stop()
        await manager.start("direct")
        third = await manager.run(job, "cache-test")
        assert third["ok"] and third["results"][-1]["value"] == "yes", third
        assert hits["/asset.js"] == 1, hits
        assert second["bytes"] < first["bytes"] and third["bytes"] < first["bytes"], (first, second, third)
        print(f"PASS cache: cold={first['bytes']} B, warm={second['bytes']} B, after relaunch={third['bytes']} B")
        print(f"PASS server hits: script={hits['/asset.js']} image={hits['/image.png']} (explicit opt-in only)")
        print("PASS profile cache survives relaunch; cached asset contributes zero proxy bytes")
    finally:
        await manager.stop()


if __name__ == "__main__":
    os.environ.setdefault("DISPLAY", ":99")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if "--proxy" in sys.argv:
        core.proxy.build_proxy = lambda kind: {"server": f"http://127.0.0.1:{server.server_port}",
                                               "username": "cache-test", "password": "test-only"}
        target = "http://cache.example.invalid/"
    else:
        target = f"http://127.0.0.1:{server.server_port}/"
    try:
        with tempfile.TemporaryDirectory(prefix="gbrowser-cache-") as profile:
            asyncio.run(main(target, profile))
    finally:
        server.shutdown()
        server.server_close()
