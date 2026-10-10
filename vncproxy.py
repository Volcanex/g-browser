"""Auth-gated reverse proxy for the noVNC live view.

Everything reaches the panel through the FastAPI app on one port, so the
single password gates the live browser view too. /vnc/ must never be exposed
straight to websockify (that would hand out the logged-in browser with no
auth). This proxies the noVNC static client (HTTP) and its RFB websocket to
the loopback websockify (127.0.0.1:6080), refusing anyone without the cookie.
"""

import asyncio

import httpx
import websockets
from fastapi import Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

import auth

UPSTREAM_HTTP = "http://127.0.0.1:6080"
UPSTREAM_WS = "ws://127.0.0.1:6080/websockify"


async def vnc_http(path: str, request: Request):
    if not auth.is_admin(request):
        return Response("login required", status_code=401)
    async with httpx.AsyncClient(timeout=30) as c:
        up = await c.get(f"{UPSTREAM_HTTP}/{path}", params=dict(request.query_params))
    return Response(
        content=up.content,
        status_code=up.status_code,
        media_type=up.headers.get("content-type"),
    )


async def vnc_ws(ws: WebSocket):
    # Cookie rides the upgrade request; reject unauthenticated before accepting.
    if not auth.is_admin(ws):
        await ws.close(code=1008)
        return
    # Echo back a subprotocol ONLY if the client offered one (noVNC 1.3 offers
    # none). Returning an unrequested subprotocol makes the browser close the
    # socket right after the handshake.
    offered = ws.scope.get("subprotocols") or []
    subp = "binary" if "binary" in offered else None
    up_subprotocols = ["binary"] if subp else None
    await ws.accept(subprotocol=subp)
    try:
        async with websockets.connect(UPSTREAM_WS, subprotocols=up_subprotocols,
                                       max_size=None, open_timeout=10) as up:
            async def c2u():
                try:
                    while True:
                        m = await ws.receive()
                        if m["type"] == "websocket.disconnect":
                            break
                        data = m.get("bytes")
                        await up.send(data if data is not None else m.get("text", ""))
                except WebSocketDisconnect:
                    pass

            async def u2c():
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            done, pending = await asyncio.wait(
                {asyncio.create_task(c2u()), asyncio.create_task(u2c())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
