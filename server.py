#!/usr/bin/env python3
"""g-browser: the FastAPI face over one always-on stealth Chrome.

Three consumer-facing pieces:
  - POST /run          job API for machine consumers (service-token gated)
  - GET  / (panel)     password-gated admin panel: live view, reseed, status
  - GET  /health       liveness + browser attach state (the /health contract)

The browser itself lives in browser/core.py; this module is just transport
and the password gate (auth.py). Nothing here touches CDP or the profile.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

import assist
import auth
import vncproxy
from browser import modes, proxy
from browser.core import SHOT_DIR, activity, manager

if not auth.COOKIE_VALUE:
    raise SystemExit(
        "g-browser refuses to start: GBROWSER_COOKIE_VALUE is unset. It is the "
        "admin session secret; set it to a long random value, for example "
        "`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`."
    )

ROOT = Path(__file__).resolve().parent
PANEL = ROOT / "panel"

# Boot into research mode: the cheap, disposable persona. Landing in `identity`
# by default would mean a restart silently arms the owner's real profile.
BOOT_MODE = os.environ.get("GBROWSER_BOOT_MODE", "research")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start(BOOT_MODE)
    yield
    await manager.stop()


app = FastAPI(title="g-browser", version="0.2.0", lifespan=lifespan)


# ---- health (unauthenticated; loopback-only in production) ----------------

@app.get("/health")
async def health():
    st = manager.status()
    # 200 whenever the process is up, even if the browser is detached. The
    # g-system /health contract keeps "service up" and "dependency healthy"
    # separate (dependency state lives in checks, not the status code).
    return {
        "ok": True,
        "checks": {
            "browser_attached": {"ok": st["attached"], "detail": st["last_error"] or "attached"},
            "proxy": {"ok": True, "detail": st["proxy"]},
        },
        "status": st,
    }


@app.get("/api/egress")
async def egress(request: Request):
    """Live egress check: does this mode's proxy work, and from which IP?

    Deliberately probed rather than inferred: an exhausted residential plan
    still looks configured, and even tunnels some HTTPS, so 'credentials
    present' is not the same as 'working'. Slow (a real HTTP round trip), so
    it is its own endpoint rather than part of /health.
    """
    auth.require_admin(request)
    name = request.query_params.get("mode") or (manager.mode.name if manager.mode else None)
    try:
        mode = modes.resolve(name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    result = await asyncio.to_thread(proxy.egress_status, mode.proxy_kind)
    return {"mode": mode.name, **result}


@app.get("/api/modes")
async def list_modes(request: Request):
    auth.require_service(request)
    return {"modes": modes.describe(), "current": manager.mode.name if manager.mode else None}


# ---- activity feed --------------------------------------------------------

@app.get("/api/activity")
async def activity_recent(request: Request):
    auth.require_admin(request)
    return {"events": activity.recent(int(request.query_params.get("n", 100)))}


@app.get("/api/activity/stream")
async def activity_stream(request: Request):
    """Server-sent events: what the browser is doing, as it does it.

    Pairs with the noVNC pane. The screen shows the pixels, this shows the
    intent, and you need both to understand why an agent did something.
    """
    auth.require_admin(request)
    q = activity.subscribe()

    async def gen():
        try:
            for ev in activity.recent(30):
                yield f"data: {json.dumps(ev)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            activity.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/shots/{name}")
async def shot(name: str, request: Request):
    """Screenshots by URL rather than inline base64: a 1080p PNG is a large
    slice of an agent's context, and the panel wants to show them anyway."""
    if not auth.is_admin(request):
        auth.require_service(request)
    if "/" in name or ".." in name:
        return JSONResponse({"error": "bad name"}, 400)
    f = Path(SHOT_DIR) / name
    return FileResponse(f) if f.is_file() else JSONResponse({"error": "no such shot"}, 404)


# ---- auth -----------------------------------------------------------------

class LoginReq(BaseModel):
    password: str


@app.post("/api/login")
async def login(req: LoginReq, request: Request, response: Response):
    return auth.do_login(req.password, request, response)


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/verify")
async def verify(request: Request):
    return {"admin": auth.is_admin(request)}


# ---- job API --------------------------------------------------------------

class RunReq(BaseModel):
    # `mode` is required by manager.run() rather than by the schema, so the
    # error a consumer gets back names the available modes instead of being a
    # bare 422. See modes.py for why there is no default.
    mode: str | None = None
    url: str | None = None
    action: str | None = None
    steps: list[dict] | None = None
    frugal: bool | str = True
    snapshot_on_error: bool = True
    # Undeclared fields are silently dropped by Pydantic before the manager ever
    # sees them, so anything a job may set has to appear here.
    timeout_s: float | None = None
    params: dict | None = None


@app.post("/run")
async def run(req: RunReq, request: Request):
    auth.require_service(request)
    holder = request.headers.get("x-gbrowser-consumer", "unknown")
    job = req.model_dump(exclude_none=True)
    # single-action shorthand: merge params into the top level
    if req.params:
        job.update(req.params)
        job.pop("params", None)
    return await manager.run(job, holder=holder)


@app.api_route("/api/assist/ack/{nonce}", methods=["GET", "POST"])
async def assist_ack(nonce: str):
    """"I have dealt with it": the fallback when the page does not say so itself.

    Unauthenticated on purpose, and the only unauthenticated write in the
    service. The nonce is the capability: 128 bits, minted per request, dead
    when that request ends, and the sole thing it can do is stop one wait early.
    The alternative was putting the service token in a push notification.
    """
    ok = assist.ack(nonce)
    body = ("<title>g-browser</title><body style='font:16px system-ui;padding:3em'>"
            + ("Thanks. The agent is carrying on." if ok
               else "Nothing is waiting on that link. It was already resolved, or it timed out.")
            + "</body>")
    return Response(body, media_type="text/html", status_code=200 if ok else 410)


@app.get("/api/status")
async def status(request: Request):
    auth.require_admin(request)
    return manager.status()


@app.post("/api/browser/restart")
async def browser_restart(request: Request):
    auth.require_admin(request)
    return await manager.restart(request.query_params.get("mode"))


@app.post("/api/browser/reseed")
async def browser_reseed(request: Request):
    auth.require_admin(request)
    # Optional multipart profile export ('profile' field). Empty = clean slate.
    tar_bytes = None
    ctype = request.headers.get("content-type", "")
    if "multipart/form-data" in ctype:
        form = await request.form()
        up = form.get("profile")
        if up is not None:
            tar_bytes = await up.read()
    return await manager.reseed(tar_bytes, request.query_params.get("mode"))


# ---- panel ----------------------------------------------------------------

# ---- noVNC live view (auth-gated reverse proxy to websockify) -------------

@app.websocket("/vnc/websockify")
async def vnc_websocket(ws: WebSocket):
    await vncproxy.vnc_ws(ws)


@app.get("/vnc/{path:path}")
async def vnc_static(path: str, request: Request):
    return await vncproxy.vnc_http(path, request)


@app.get("/login")
async def login_page():
    f = PANEL / "login.html"
    return FileResponse(f) if f.is_file() else JSONResponse({"error": "no login page"}, 500)


@app.get("/")
async def panel_index(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/login", status_code=302)
    f = PANEL / "index.html"
    return FileResponse(f) if f.is_file() else JSONResponse({"error": "no panel"}, 500)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8098"))
    uvicorn.run("server:app", host=host, port=port, reload=os.environ.get("DEBUG", "").lower() in ("1", "true"))
