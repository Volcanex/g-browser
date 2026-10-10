"""Sticky-proxy proof. Confirms egress via the Webshare residential IP rather
than the host's own. Needs WEBSHARE_RESIDENTIAL_* in env (source passport)."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from browser.core import manager
from browser.proxy import proxy_label

async def main():
    print("proxy:", proxy_label())
    await manager.start()
    print("attached:", manager.attached, "| err:", manager.last_error)
    if not manager.attached:
        return 1

    ip = await manager.run({"url": "https://api.ipify.org?format=json",
                            "steps": [{"action": "read_text", "selector": "body"}]}, holder="smoke")
    print("egress IP:", ip.get("results"))
    await manager.stop()
    return 0

sys.exit(asyncio.run(main()))
