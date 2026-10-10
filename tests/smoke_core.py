"""Direct smoke test of BrowserManager: launch headful Chrome under Xvfb,
navigate, read. Run with DISPLAY=:99. No proxy (isolates launch/attach)."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from browser.core import manager

async def main():
    await manager.start()
    print("attached:", manager.attached, "| err:", manager.last_error)
    if not manager.attached:
        return 1
    r = await manager.run({"url": "https://example.com", "steps": [
        {"action": "read_text", "selector": "h1"},
    ]}, holder="smoke")
    print("job:", r)
    await manager.stop()
    return 0 if r.get("ok") else 1

sys.exit(asyncio.run(main()))
