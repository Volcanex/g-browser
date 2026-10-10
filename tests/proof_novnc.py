"""Prove the live-view chain: load the noVNC client (headless) and screenshot
what it renders. If it shows the real gbrowser's current page (example.com),
then x11vnc(:99) -> websockify(6080) -> noVNC is working end to end."""
import asyncio, sys
from patchright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        pg = await b.new_page()
        await pg.goto("http://127.0.0.1:6080/vnc_lite.html?path=websockify&autoconnect=1&resize=scale",
                      wait_until="load")
        await pg.wait_for_timeout(6000)  # let RFB connect + paint
        await pg.screenshot(path="/tmp/novnc_proof.png")
        # noVNC sets the document title to the desktop name once connected
        print("novnc page title:", await pg.title())
        await b.close()

asyncio.run(main())
print("screenshot -> /tmp/novnc_proof.png")
