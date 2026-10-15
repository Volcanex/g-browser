# g-browser

Was `gbrowser` until 2026-10-09. The CLI command, the systemd units, the
`GBROWSER_*` env vars, the `X-Gbrowser-*` headers, the cookie name, the
`gbrowser-driving` skill and the `gbrowser` compat symlink on h keep the
old name, because consumers and live config call them by it.

- **Owner:** Gabriel Penman
- **Host:** h
- **Service:** `gbrowser`
- **URL:** per instance, `GBROWSER_PANEL_URL`
- **Port:** 8098
- **Licence:** GNU AGPL v3.0 only (`LICENSE`), open source. Third-party code is listed in `THIRD_PARTY_NOTICES`.

One always-on, headful Chrome, exposed as a job service. Built because
G-System keeps hitting **human-only UIs** that have no API, such as a supplier
dashboard, a marketplace listing or a web form. Any g-project sends
`POST /run` and gets a result; the stealth, the egress and the identity stay
sealed in here.

g-browser is **the hand, not the brain**. It perceives and acts; deciding what to
do lives in the consumer. The accounts it drives are the owner's own and were
logged in by the owner, and it acts on the owner's behalf to automate their own work.

## Public scraping and fallback

Research mode was retired for agents on 2026-10-14. Public scraping belongs in
`g-scraper`. Keep `research` available for existing g-jobs harvest callers during
the migration. Research-mode CLI calls without `--fallback` print this warning
on stderr:

```text
the research browser is retired for agents: use g-scraper; --fallback for the hardest walls
```

Use `--fallback` only for the hardest bot walls that g-scraper cannot pass:

```sh
gbrowser snapshot --mode research --fallback --url https://example.com
gbrowser screenshot --mode research --fallback --labels
```

The API equivalent is `"fallback": true`. Flagged non-identity jobs share a
server-side rolling-hour budget of 20 jobs, configured by the positive integer
`GBROWSER_FALLBACK_PER_HOUR`. Each admitted job counts, including failures and
jobs waiting for a browser tab. Identity jobs are exempt. Unflagged legacy
jobs keep working during migration.

Admission is persisted in `var/fallback.sqlite3` (under `GBROWSER_VAR` when set),
so restarting never resets the budget. Excess jobs are refused before waiting for
a browser tab with HTTP 429, `Retry-After`, and CLI exit **9**. The error
names the UTC time when enough slots expire; JSON also includes `retry_at` and
`retry_after_s`. Stop and defer until that time.

Fallback jobs force `frugal: "strict"`, even if a caller asks to disable it.
Images, media and fonts are blocked. A screenshot step with `label_refs: true`
(`screenshot --labels`) instead uses normal frugal blocking for the whole job:
images are allowed, while media and fonts remain blocked. Navigate in that same
labelled screenshot job if images were blocked by an earlier job.

Job results include `bytes`. `gbrowser status` shows the last job and every
fallback job in the rolling hour with its byte count and budget usage. These
are CDP encoded transfer bytes, including chunked responses and response headers;
HTTP-cache and service-worker hits count as zero. Proxy/TLS overhead is not measured.
Each tab has its own meter; responses from another tab never enter its job total.
Fallback records persist across restarts; the last-job and cumulative bandwidth
summaries are in memory.

## Modes: which persona is wearing the browser

A mode bundles the three things that together make an identity: exit IP, cookie
jar, fingerprint. They are bundled because they only make sense together -
The owner's cookies leaving from a datacenter IP is worse than either alone.

| mode | profile | egress | acts as the owner |
|---|---|---|---|
| `identity` | `var/profile` | static residential | **yes** |
| `research` | `var/profile-research` | datacenter | no |
| `direct` | `var/profile-research` | bare host IP | no, dev only |

**Every job must name its mode.** There is no default: defaulting would mean a
consumer that forgot to think about it silently borrows the owner's identity, and
that is the failure this design exists to prevent. An unnamed mode is refused
with a message listing the options.

Switching mode waits for every busy tab to drain, then **relaunches Chrome**.
Patchright fixes both proxy and profile at `launch_persistent_context` time.
Batch work by mode. The service boots into
`research` so a restart never silently arms the identity profile.

Honest limit: both modes run the same Chrome on the same Xvfb display, so canvas,
WebGL and font fingerprints are identical between them. The viewport differs. The
real separators are the IP and the cookie jar - treat fingerprint variation as
friction, not a wall.

## Why it's shaped this way (not implicit in the code)

- **Patchright, not vanilla Playwright or nodriver.** Patchright is patched
  Playwright (fixes the `Runtime.enable` fingerprint leak) - raw-CDP under the
  hood, but keeps Playwright's **native authenticated-proxy** support, which
  Webshare requires and nodriver lacks. An earlier bot already proved this
  stack against DataDome.
- **Headful + real system Chrome (`channel="chrome"`) is mandatory.** Headless is
  an instant tell to DataDome. Runs on an **Xvfb** display (`:99`); a real window
  exists so noVNC can show it and the owner can take over for logins.
- **The UA is not pinned.** It used to claim Chrome 126 while the binary was 150 -
  a mismatch every fingerprinting script cross-checks against the Client Hints
  and the JS feature surface. An honest Chrome reports itself. `GBROWSER_UA`
  overrides only if you have a reason.
- **Up to three jobs at once, on separate tabs.** `GBROWSER_MAX_TABS` is a
  positive integer (default 3). One persistent context shares cookies and egress;
  each job leases one tab with its own refs, signatures, status and byte meter.
  A FIFO condition queue admits jobs; a queued mode switch waits for all busy
  tabs and prevents new jobs jumping ahead. Explicit follow-ups on one tab
  serialise. Restart/reseed refuse while any tab is busy.
- **Tab IDs preserve multi-call flows.** Every admitted job returns `tab_id`.
  Pass `--tab ID` on CLI follow-ups, or `params: {"tab_id": "ID"}` on `/run`.
  Without an ID a consumer's sole tab is reused; concurrent navigation jobs can
  create more tabs, after which an ambiguous follow-up fails closed. IDs expire
  on relaunch, eviction or closure: an unknown ID never falls back to another
  page. Idle extras close after 60 seconds; at least one tab remains. New work
  may evict an idle tab at capacity. `status.tabs` reports busy/total/max and
  each session's holder, URL and start time.
- **The profile is the real thing.** `var/profile/` IS the owner's identity, seeded
  from their desktop Chrome and maintained via the panel's re-seed. Only the
  orchestrator ever opens it - Chrome locks the dir; two openers corrupt it.
- **Every job has a wall-clock budget.** Found the hard way: a Cloudflare
  interstitial that keeps re-navigating blocks `frame.evaluate` indefinitely, and
  one such page wedged the shared browser for four minutes. Job budget (150s
  default, `timeout_s` per job), step ceiling, and a timeout on every page-side
  evaluate. A stuck job must never take the browser down with it.
- **Challenge detection reads the title and headings, never the whole body.**
  Ordinary prose uses the same words as a block page ("access denied", "rate
  limit" and friends), so matching them anywhere in the page text reports walls
  on healthy pages. A block page says so in its `<title>` or an `<h1>`/`<h2>`, so
  that is where it is looked for; a body match only counts on a page under 1200
  characters, which a real content page never is. **A false wall is not a safe
  failure.** Exit 3 stops work and triggers a backoff, so a detector that cries
  wolf costs exactly as much as one that misses. If you widen these patterns,
  test them against a page that merely *discusses* rate limits.
- **Identity bandwidth is opt-in for images.** Identity always blocks media,
  fonts, ping/beacon requests and known third-party analytics/ads/tracker hosts.
  First-party scripts stay available; DataDome, Cloudflare challenge, hCaptcha,
  reCAPTCHA and other challenge hosts are exempt from these filters. Pass
  `--images` (API `params: {"images": true}`) for screenshot/upload flows that
  need images; navigate in that job if a prior load blocked them. Research keeps
  normal frugal media/font blocking, with strict image blocking for fallback
  jobs except labelled screenshots.
- **HTTP caching stays enabled.** CDP Fetch filtering replaces Playwright
  routing, which disables HTTP caching. Chrome keeps a 256 MiB HTTP disk cache
  in each profile's `http-cache/`, across jobs and relaunches. Cache freshness
  follows origin headers. Byte accounting uses CDP encoded transfer lengths,
  counts cache hits as zero and exposes per-job `bandwidth.by_host` and
  `bandwidth.by_resource_type`.
  The filtering CDP session also answers proxy auth challenges; enabling Fetch
  without this breaks authenticated proxies. This session supplies credentials
  only for proxy challenges, never for a server's HTTP authentication challenge.

## Perception: how a weak agent drives it

`{"action":"snapshot"}` walks every frame and returns a numbered outline:

```
[e20]     textbox   "Email"  (required, empty)
[e24]     button    "Attach"
[e57]     button    "Submit application"
```

Actions then take `ref:"e20"` instead of a selector. Three things are
load-bearing:

- **Refs fail closed.** Each carries a signature (role + accessible name) taken
  at snapshot time; resolving re-checks `isConnected` *and* the signature,
  because React reuses DOM nodes for different controls. A stale ref clicking the
  wrong control on a real form is the worst thing this service can do.
- **Frames are namespaced, not flattened** (`f1e3`). ATS apply forms live in
  iframes; a snapshot that stops at the main document sees no form at all.
- **The ref map lives in the page** (`window.__gb.refs`), so it dies with the JS
  context on navigation - which is exactly the invalidation we want, for free.

Screenshots go to `var/shots/` and return a URL, not base64 (a 1080p PNG is a
large slice of an agent's context). `label_refs:true` draws the `e`-numbers onto
the image so a confused agent can map what it sees to what it can click; badges
are drawn inside each frame's own document, so the browser composites them
correctly at any nesting depth.

**Failures are soft.** A failed step returns everything that succeeded before it,
the index that failed, and an automatic snapshot of where the page ended up.
`"optional": true` on a step downgrades it to a skip. The old behaviour - discard
everything, return a timeout string - left an agent with no way to recover.

**Challenge detection is perception, not policy.** `check_challenge` (and every
snapshot) reports `{kind, confidence, evidence}` for Cloudflare, DataDome,
hCaptcha, reCAPTCHA, PerimeterX, auth walls and generic block pages. Confidence
matters: a passive reCAPTCHA badge on a form is `low` and is not a block. What to
*do* about a wall - cold-down, rate budgets, alerting - belongs to the governor,
which does not exist yet.

## Assist: "a human is needed at this browser session"

`gbrowser assist --mode identity --reason "Turnstile checkbox"` pushes to
the owner's phone via the ntfy spine and waits (default 120s). It lives here, not
in a consumer, because the session, the lock and challenge detection are all
here. `assist.py` owns it; `cli.py` just wires the subcommand.

Exit codes are the interface: **0** a human dealt with it, **7** nobody came,
**8** deferred (quiet hours), **1** could not ask.

- **02:00-08:00 defers instead of waiting.** assist blocks its caller, and the
  caller is usually holding a queue; a 40-job overnight run cannot spend two
  minutes per wall waiting for someone who is asleep. It returns 8 immediately,
  having pinged nothing. The window is **Europe/London**, not host-local - h
  runs on UTC, so host-local would drift an hour every summer
  (`GBROWSER_ASSIST_TZ` overrides).
- **`--mode` is asserted, never applied.** A mismatch exits 1. Applying it would
  relaunch Chrome and destroy the page the human was summoned to.
- **Assist needs an unambiguous tab.** With one open tab it observes that session
  without taking over the consumer's ownership. Multiple tabs are refused until
  the idle extras close; it must never observe or acknowledge the wrong form.
- **Resolution is read off the page, then acked.** Each poll runs
  `check_challenge` + one `eval` in a single job (one lock acquisition) and looks
  for: the detected wall clearing, a captcha response field
  (`cf-turnstile-response` and friends) going from empty to filled, or the URL
  moving. Two known blind spots - `detect()` has no rule for an embedded
  Turnstile *checkbox* (only full-page interstitials), and `eval` runs in the
  main document, so a form inside a cross-origin ATS iframe is unreadable. Hence
  the ack: the push carries `POST /api/assist/ack/{nonce}` as a tappable action.
- **The ack route is the only unauthenticated write in the service.** The nonce
  is the capability - 128 bits, one request, deleted when the wait ends, and all
  it can do is end that wait early. The alternative was putting the service token
  in a push notification.
- **`var/assist/log.jsonl`** records every request (reason, url, outcome, seconds
  waited, which signal fired). This is the honest number for how often the
  automation cannot finish alone.
- ntfy is reached by reading `NTFY_URL` the same defensive way g-brain does
  (env, then the passport, `.env` here and g-brain's `.env`). The passport is
  found the way g-env finds it: `GCORE_ENV`, `GCORE_DIR`, `G_INSTANCE`, then a
  `gp-core` or `gcore` checkout beside this one. g-browser deliberately does
  **not** import g-brain:
  the browser should not depend on the knowledge base for a URL, and the spine's
  `send()` has no Click/Actions headers, which is the whole point here.
- The push links (Click, Done, Open browser) point at `GBROWSER_PANEL_URL`, the
  panel's public URL. It is per instance and has no default. Without it the push
  still goes out with no links, and assist says so. The CLI sends the push and
  does not get the systemd drop-in, so set it in `.env` here.
- No rate limit: a wall that recurs on every job in a queue pushes per job.

## Loaded gun

The panel controls the owner's fully logged-in identity. Non-negotiable:
single-password gate (`auth.py`, `GBROWSER_PASSWORD`), debug port bound to
loopback only: **never expose 9222**. Machine consumers of `/run` use
`GBROWSER_SERVICE_TOKEN` (`X-Gbrowser-Token`), not the cookie.

**The admin cookie value is a secret.** Login sets `gbrowser_admin` to
`GBROWSER_COOKIE_VALUE`, and holding that value is being logged in. It was once
the constant `"ok"`, which made the cookie forgeable from the public hostname.
There is no default: `server.py` refuses to start without it, and an unset value
never matches.

**Uploads are confined to `var/uploads/`.** Without that restriction anything
holding the service token could attach the passport (`gp-core/.env`) to a web
form and exfiltrate it in one call.

## Fetch: rotating-IP HTTP

`gbrowser fetch --mode research --url …` does a plain HTTP GET through a mode's
egress and prints JSON (`url`, `http_status`, `exit_ip`, `bytes`, `body`).

**No Chrome, no lock, no job queue.** It runs client-side in the CLI process, so
it does not queue behind whoever is holding the browser and cannot be starved by
a long-running session.

This is a supported path, not legacy: it is the rotating datacenter-IP pool, the
one thing g-scraper (bare host IP, no proxy) does not have. g-jobs pages through
LinkedIn's guest search with it, where a single IP would be rate limited. Use it
for high-volume fetches that need IP rotation; use g-scraper for everything else
public. It does not submit a browser job, take the lock, consume the fallback
budget, or print the retirement warning.

`--rotate` takes a new exit IP first. It is **refused for `identity`**, and that
refusal is load-bearing: identity's `cf_clearance` and `datadome` cookies are
bound to the IP that earned them, so a profile whose egress teleports self-flags.
Rotate the datacenter pool freely, since nothing on it is the owner.

## Egress

Two Webshare products, not interchangeable - see `browser/proxy.py` for the full
reasoning. Two gotchas worth not rediscovering:

- **`plan_id` is the only filter that works** on `/api/v2/proxy/list/`.
  `proxy_type` and `plan` are silently ignored and return the *other* plan's
  proxies - which is how an afternoon went on believing the ISP IPs had never
  been provisioned when they were there all along. Plan ids are discovered by
  `proxy_subtype` (`default` / `isp`), never hardcoded.
- The `p.webshare.io` backbone serves the **rotating residential** product only.
  Datacenter and static-residential proxies are addressed individually as
  `IP:PORT`; pointing their credentials at the backbone returns nothing at all,
  silently.
- An exhausted plan answers `Bandwidth limit reached` in a plain-HTTP body but
  *intermittently still tunnels HTTPS*. "Some requests work" is not evidence of
  a healthy plan. `GET /api/egress?mode=…` probes properly and reports the live
  exit IP - and does it without launching the browser, so identity egress can be
  checked without arming the identity profile.

Credentials come from `WEBSHARE_API_KEY` via the proxy list (each entry carries
its own username/password) and are cached in `var/webshare_{datacenter,static}.json`
(mode 600) - no extra secret in the passport, no launch-time dependency on the
Webshare API.

The static plan auto-replaces its IPs monthly unless that is turned off in the
Webshare dashboard. For `identity` that is a forced cookie burn every 30 days;
`_session_index` holds one IP and `rotate()` is the deliberate way to move.

## Layout

- `server.py`: FastAPI: `/run` (job API), `/` panel, `/health`, `/api/egress`,
  `/api/activity{,/stream}`, `/shots/{name}`, `/api/assist/ack/{nonce}`, auth
  endpoints.
- `auth.py`: single-password cookie gate + service-token gate.
- `assist.py`: the human-intervention primitive: quiet hours, the ntfy push,
  page-side resolution detection, the ack nonce, the JSONL record.
- `browser/core.py`: `BrowserManager`: launch/attach, tab leases, timeouts, the action
  set, the bandwidth meter, the activity ring buffer.
- `browser/fallback.py`: persistent rolling-hour fallback admission and per-job
  byte records.
- `browser/modes.py`: the three personas and the no-default rule.
- `browser/snapshot.py`: the injected perception library: ref walker, ref
  resolution, challenge detection, screenshot overlay.
- `browser/proxy.py`: egress per mode, and the Webshare gotchas.
- `deploy/`: example systemd units (placeholders, see Run) and an nginx example.
- `panel/`: login + admin panel (noVNC live view, mode badge, activity feed).
- `var/profile{,-research}/`: persistent Chrome profiles (gitignored).
- `var/{shots,downloads,uploads}/`: screenshot, download and upload areas.

## Run

`gbrowser` systemd unit runs uvicorn from this checkout (edits go live on
restart - commit before restarting). Needs the display stack up first:
`gbrowser-xvfb` → `gbrowser-x11vnc` → `gbrowser-novnc` → `gbrowser`.

The units in `deploy/` are examples with `@USER@`, `@REPO@` and `@INSTANCE_ENV@`
placeholders. The live units in `/etc/systemd/system/` are the instance's own
filled-in copies, not links into this repo, so editing `deploy/` changes nothing
live. A change meant for the host is made in both places, then
`systemctl daemon-reload`.

Env comes from the passport (`@INSTANCE_ENV@`, usually `gp-core/.env`) then
`.env` here: `GBROWSER_PASSWORD`, `GBROWSER_SERVICE_TOKEN`,
`WEBSHARE_*`, `GBROWSER_PANEL_URL`. Profiles, shots and assist state live in
`var/` beside this file unless `GBROWSER_VAR` points elsewhere. The admin cookie
secret `GBROWSER_COOKIE_VALUE` comes from `/etc/g-secrets/gbrowser-admin.env`
(or `.env` here). Without it the service will not start.
`GBROWSER_FALLBACK_PER_HOUR` defaults to 20 and `GBROWSER_MAX_TABS` defaults to 3;
both can be set in `.env` here.

Tests: `.venv/bin/python -m pytest tests/test_unit.py` (pure logic),
`.venv/bin/python tests/smoke_cache.py` (isolated profile/local HTTP cache), and
`.venv/bin/python tests/smoke_phase1.py` plus `tests/smoke_tabs.py` against the live
service. `tests/measure_identity.py before|after` records read-only identity
loads by type/host with an eight-second observation window and a cold cache.
Use `tests/smoke_cache.py --proxy` to check cache reuse through an authenticated
local proxy, including image blocking and the explicit image opt-in.
It clears HTTP cache only, leaving cookies/login state intact. Recorded samples
live in `tests/identity_{before,after}.json`; Indeed's `.co.uk` and `.com` hosts
are both counted as first party, while separate CDN domains count as third party.

## CLI

`gbrowser` (on PATH, `cli.py`) is the ergonomic front end and the reason there is
no MCP server: the consumer runs on this host, so the OAuth and tunnel cost brain
pays for its MCP face buys nothing here. `POST /run` stays the seam if that ever
changes.

```sh
gbrowser snapshot --mode identity --url https://example.com/apply
gbrowser click --mode identity --tab ID --ref e24 --then-snapshot
gbrowser upload --mode identity --tab ID --images --ref e24 --chooser --file var/uploads/document.pdf
gbrowser status | egress --mode identity | modes
```

`--mode` is required, mirroring the no-default rule. Exit codes: **0** ok,
**1** failed, **3** bot wall detected - distinct so a shell loop can tell "this
failed" from "stop, we have been walled". Retrying into a challenge is how IPs
get burned. **9** means the fallback budget is exhausted; defer until the time
reported in the error. `assist` adds **7** (nobody came) and **8** (quiet hours).

The driving skill lives in `skills/gbrowser-driving/`, symlinked into
`g-env/claude/skills/` so any session loads it.

## Job API

`POST /run` with a mode and either a single action or a sequence:

```json
{ "mode": "identity",
  "url": "https://job-boards.greenhouse.io/example/jobs/123",
  "timeout_s": 150,
  "params": {"images": true},
  "steps": [
    {"action": "snapshot"},
    {"action": "fill", "ref": "e20", "value": "..."},
    {"action": "upload", "ref": "e24", "via": "chooser", "files": ["var/uploads/document.pdf"]},
    {"action": "screenshot", "label_refs": true},
    {"action": "click", "ref": "e57", "optional": true}
  ]}
```

Actions: `snapshot`, `check_challenge`, `goto`, `back`, `reload`, `click`,
`hover`, `focus`, `fill`, `type`, `clear`, `press`, `select_option`, `scroll`,
`scroll_into_view`, `upload`, `download`, `wait_for`, `wait`, `read_text`,
`read_html`, `get_attribute`, `query_all`, `eval`, `screenshot`.

Most accept `ref` **or** `selector`. The selector path is unchanged, so an
earlier bot's hard-won choreography still works verbatim. Site choreography
lives in the **consumer**; g-browser stays site-agnostic.

## Identity bandwidth measurements (2026-10-15)

Read-only live identity navigation, normal defaults, one cold-cache sequence per
configuration, eight seconds after DOMContentLoaded. No login, submission or
external click. CDP encoded transfer lengths include response headers and exclude
proxy/TLS overhead. The after observer uses a raw CDP socket: a second Patchright
client disturbed target proxy-auth state. Both samples use Network events.
Dynamic content and challenge responses vary between loads. Separate CDN domains
(for example licdn.com) count as third party; this does not mean they are trackers.
Indeed redirected to uk.indeed.com, counted alongside indeed.co.uk as first party.
Indeed reported a login wall in both samples; Vinted reported Cloudflare in both.
The Vinted numbers describe the challenge, not the listing content.

| Page | Before bytes | After bytes | Reduction | First-party before → after | Third-party before → after |
|---|---:|---:|---:|---:|---:|
| LinkedIn jobs | 8,479,728 | 7,380,279 | 12.97% | 545,173 → 603,251 | 7,934,555 → 6,777,028 |
| Indeed UK | 1,926,124 | 1,704,468 | 11.51% | 1,534,998 → 1,497,379 | 391,126 → 207,089 |
| Vinted listing URL | 446,340 | 426,171 | 4.52% | 389,755 → 397,791 | 56,585 → 28,380 |

Bytes by resource type (before → after):

| Type | LinkedIn | Indeed | Vinted |
|---|---:|---:|---:|
| Document | 198,337 → 195,027 | 79,400 → 70,752 | 12,620 → 13,442 |
| FedCM | 0 → 0 | 79,257 → 0 | 0 → 0 |
| Fetch | 1,042,740 → 276,848 | 52,103 → 51,635 | 0 → 0 |
| Image | 266,893 → 0 | 7,736 → 0 | 0 → 0 |
| Media | 0 → 0 | 0 → 0 | 0 → 0 |
| Font | 0 → 0 | 0 → 0 | 0 → 0 |
| Other | 1,386 → 1,321 | 2,603 → 2,540 | 0 → 0 |
| Ping | 0 → 0 | 7,174 → 0 | 0 → 0 |
| Script | 6,366,955 → 6,261,449 | 1,603,934 → 1,487,660 | 266,828 → 241,670 |
| Stylesheet | 398,614 → 440,769 | 89,802 → 88,505 | 0 → 0 |
| XHR | 204,803 → 204,865 | 4,115 → 3,376 | 166,892 → 171,059 |

Bytes by host, LinkedIn jobs:

| Host | Party | Before bytes | After bytes |
|---|---|---:|---:|
| static.licdn.com | third | 6,983,764 | 6,420,128 |
| www.linkedin.com | first | 460,839 | 518,928 |
| www.gstatic.com | third | 355,346 | 355,346 |
| www.googletagmanager.com | third | 316,779 | 0 |
| media.licdn.com | third | 264,784 | 0 |
| platform.linkedin.com | first | 83,379 | 83,369 |
| googleads.g.doubleclick.net | third | 3,429 | 0 |
| dpm.demdex.net | third | 2,094 | 0 |
| www.google.com | third | 1,698 | 1,554 |
| 9261636.fls.doubleclick.net | third | 1,615 | 0 |
| lnkd.demdex.net | third | 1,511 | 0 |
| merchantpool1.linkedin.com | first | 955 | 954 |
| 10880300.fls.doubleclick.net | third | 944 | 0 |
| collect.tealiumiq.com | third | 763 | 0 |
| trkn.us | third | 721 | 0 |
| www.google.co.uk | third | 455 | 0 |
| bat.bing.com | third | 378 | 0 |
| www.facebook.com | third | 274 | 0 |

Bytes by host, Indeed UK:

| Host | Party | Before bytes | After bytes |
|---|---|---:|---:|
| c03.s3.indeed.com | first | 1,098,142 | 1,084,849 |
| sgtm.indeed.com | first | 320,600 | 320,720 |
| d3fw5vlhllyvee.cloudfront.net | third | 207,077 | 207,089 |
| accounts.google.com | third | 163,622 | 0 |
| uk.indeed.com | first | 104,487 | 85,793 |
| lh3.googleusercontent.com | third | 19,895 | 0 |
| autocomplete.indeed.com | first | 5,564 | 4,887 |
| secure.indeed.com | first | 4,190 | 0 |
| s.indeed.com | first | 1,153 | 696 |
| t.indeed.com | first | 543 | 115 |
| google.com | third | 532 | 0 |
| www.indeed.co.uk | first | 319 | 319 |

Bytes by host, Vinted listing URL:

| Host | Party | Before bytes | After bytes |
|---|---|---:|---:|
| www.vinted.co.uk | first | 389,755 | 397,791 |
| challenges.cloudflare.com | third | 56,585 | 28,380 |

Verification:
- Unit suite: 66 passed, one existing Starlette/httpx deprecation warning.
- Existing live smoke suite: 22 passed, 0 failed.
- Live refs: two synthetic forms retain separate refs/values; ambiguous calls refuse.
- Live identity: three distinct tabs overlap for 5.359 seconds; all exit 195.40.133.26.
- Live mode drain: a queued research job starts 0.695 seconds after the last
  identity tab finishes; the mode stays identity while three tabs are busy.
- Live idle cleanup: extras close to 0 busy / 1 total, with the cap still 3.
- Isolated local cache: 20,485 B cold, 418 B warm, 239 B after relaunch;
  the server received one script request and one image request (explicit opt-in).
- Authenticated local proxy/cache: 20,669 B cold, 418 B warm, 423 B after relaunch;
  one script request across relaunch; the filter's auth handler only answers proxy challenges.
