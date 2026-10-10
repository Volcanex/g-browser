"""Passive check the full vnc.html client connects + renders through the
auth-gated proxy. View-only: sets no input, won't disturb the real browser."""
import asyncio, os, sys
from patchright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context()
        # is_admin only checks cookie value; secure flag irrelevant for the test.
        # Run with the service's GBROWSER_COOKIE_VALUE in the environment.
        await ctx.add_cookies([{"name":"gbrowser_admin","value":os.environ["GBROWSER_COOKIE_VALUE"],
                                "domain":"127.0.0.1","path":"/"}])
        pg = await ctx.new_page()
        await pg.goto("http://127.0.0.1:8098/vnc/vnc.html?path=vnc/websockify&autoconnect=true&resize=scale",
                      wait_until="load")
        await pg.wait_for_timeout(7000)
        await pg.screenshot(path="/tmp/fullvnc_proof.png")
        # noVNC status bar text tells us if it connected
        status = await pg.evaluate("(document.getElementById('noVNC_status')||{}).textContent || ''")
        print("noVNC status:", repr(status))
        print("has keyboard button:", await pg.evaluate("!!document.getElementById('noVNC_keyboard_button')"))
        await b.close()

asyncio.run(main())
print("screenshot -> /tmp/fullvnc_proof.png")
