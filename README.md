# g-browser

One always-on, headful Chrome on a server, run as a job service for G-System.
Agents keep running into pages that only a person is meant to use, such as a
supplier's "source product" button or a long multi-step form. Any project on the
host can send `POST /run` with a list of steps and get the result back. The
stealth, the proxy egress and the logged-in profile stay inside this service.

This repo is source-available under the PolyForm Noncommercial 1.0.0 licence.
It is not open source.

## How it works

Every job names a mode, and a mode bundles an exit IP, a cookie jar and a
fingerprint. There is no default mode, so a caller that forgot to choose can't
end up borrowing the owner's real identity. Agents drive pages through
accessibility snapshots with short refs (`e24`), which is cheaper and steadier
than raw selectors. A password-gated panel shows the live browser over noVNC
next to a feed of what the agents are doing, and `gbrowser assist` pings a phone
when a human is needed.

The `gbrowser` CLI wraps the job API for agents on the same host. `CLAUDE.md` has
the full design, the gotchas and the job reference.

## Running it

It runs under systemd on Linux, with Xvfb, x11vnc and noVNC as the display stack.
Example unit files are in `deploy/`. Fill in their placeholders, copy them to
`/etc/systemd/system/` and enable them. It needs three secrets in its environment:
`GBROWSER_PASSWORD` for the panel, `GBROWSER_SERVICE_TOKEN` for `/run`, and
`GBROWSER_COOKIE_VALUE`, the value the login cookie is set to. The server won't
start without the cookie value. `openssl rand -hex 32` makes a good one.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/patchright install chrome
.venv/bin/python3 -m pytest tests/test_unit.py
```

## Licence

PolyForm Noncommercial 1.0.0, see `LICENSE`. Copyright Gabriel Penman.
Dependencies keep their own licences, listed in `THIRD_PARTY_NOTICES`.
