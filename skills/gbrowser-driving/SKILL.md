---
name: gbrowser-driving
description: Use for logged-in identity tasks such as filling forms, applying to jobs, or operating a UI with no API, and as a throttled fallback for the hardest bot walls g-scraper cannot pass. Covers the gbrowser CLI, refs, fallback budgets, and stopping on challenges. Public scraping belongs in g-scraper; research mode is retired for agents except explicit fallback jobs.
---

# Driving gbrowser

One shared headful Chrome on host h. Every consumer takes turns on it, so hold it
briefly and let go. It is **the hand, not the brain**. It perceives and acts,
you decide.

## Before you reach for it

Public scraping belongs in `g-scraper`. Research mode was retired for agents on
2026-10-14, but remains available for existing g-jobs harvest callers during the
migration. Use gbrowser for identity tasks, or explicitly as a throttled fallback
for the hardest bot walls g-scraper cannot pass.

The browser is metered in identity mode and serves one caller at a time. If the
data is available from a JSON endpoint, use that through the public scraping
path. Greenhouse, Lever and Ashby all publish public job boards as JSON.
Reading listings through a 2MB single-page app when a 20KB API exists is the most
common way to waste this resource.

Use identity mode for filling and submitting forms under the owner's name,
logged-in sessions, and other tasks a human would have to click.

## Modes: pick deliberately

    research   browser retired for agents (--fallback only); `fetch` stays as the rotating-IP HTTP pool
    identity   the owner's real profile, static residential IP, acts as them
    direct     no proxy, bare host IP, development only

`--mode` is required and has no default. That is on purpose: a caller who forgets
to think about it would otherwise borrow the owner's identity by accident.

Use `identity` when the task requires being the owner. For public scraping use
g-scraper, and only use `research --fallback` when it cannot pass a hard wall.
Switching mode relaunches Chrome (~10s), so batch work by mode rather than
alternating.

## Throttled fallback

```sh
gbrowser snapshot --mode research --fallback --url https://example.com
gbrowser screenshot --mode research --fallback --labels --url https://example.com
```

Every browser job in the fallback flow needs `--fallback`, including follow-up
clicks, reads and screenshots. API consumers send `"fallback": true` on `/run`.
Flagged non-identity jobs share 20 admissions per rolling hour, configured by
`GBROWSER_FALLBACK_PER_HOUR`. Failed and queued jobs count too. The server
persists admissions and byte records in `var/fallback.sqlite3`, so a restart
does not reset the budget. Identity jobs are exempt.

Exit **9** means the budget is exhausted. Stop and defer until the UTC time in
the error; JSON includes `retry_at` and `retry_after_s`. The API returns HTTP
429 with `Retry-After`. Refusal happens before waiting for the browser lock.

Fallback jobs enforce strict blocking of images, media and fonts. A labelled
screenshot allows images for its whole job while still blocking media and
fonts. If an earlier strict job blocked images, navigate again in the labelled
screenshot job. `--frugal false` cannot override fallback blocking.

`gbrowser status` shows budget usage and bytes for each fallback job in the
rolling hour; job results also include `bytes`. Byte counts are Content-Length
estimates, so chunked responses and proxy overhead are not measured.

Research-mode CLI calls without `--fallback` still work for compatibility but
warn on stderr: `the research browser is retired for agents: use g-scraper; --fallback for the hardest walls`.

## The loop

Snapshot, act on a ref, verify. Never act blind.

```sh
gbrowser snapshot --mode identity --url https://example.com/apply
#   [e20] textbox "Email"  (required, empty)
#   [e24] button  "Attach"
#   [e57] button  "Submit application"

gbrowser fill --mode identity --ref e20 --value "someone@example.com"
gbrowser click --mode identity --ref e24 --then-snapshot
```

`--then-snapshot` acts and re-perceives in a single job, which saves a round trip
and a second turn on the lock. Use it whenever you expect the page to change.

**Refs die on navigation.** They are per-snapshot, and the service refuses a
stale one rather than guessing. If you get `not in the ref map`, that is the
system working. Re-snapshot and carry on. Never retry a stale ref, and never fall
back to a CSS selector to "get around" a stale-ref error, because the thing it is
protecting you from is clicking the wrong control.

## When you get confused

```sh
gbrowser screenshot --mode identity --labels
```

Draws the `e`-numbers onto the image, so what you see maps to what you can click.
Returns a URL rather than inline base64. Pass `--inline` only if you truly need
the bytes, because a 1080p PNG is a large slice of your context.

## Backing off

```sh
gbrowser challenge --mode identity     # exit 3 means walled
```

Every command exits **3** when a bot wall is detected, distinct from **1** for an
ordinary failure. Treat 3 as a full stop:

- **Do not retry.** Retrying into a challenge is how residential IPs get burned.
- **Do not switch modes to get around it.** That attaches the owner's identity to
  whatever tripped the wall.
- Report what happened, name the wall kind, and stop.

A `low` confidence result (usually a passive reCAPTCHA badge) is not a wall and
does not exit 3. Carry on.

## Asking for a human

When the wall is the sort a person clears in five seconds (a Turnstile
checkbox, a login, an SMS code), ask for one instead of giving up:

```sh
gbrowser assist --mode identity --reason "Turnstile checkbox on the apply form"
```

It pushes to the owner's phone with a link to the live browser (the panel at
`GBROWSER_PANEL_URL`), then waits up to 120s (`--timeout`), watching the page
for their work landing.

- **0**: he dealt with it. Snapshot again and carry on from where you were.
- **7**: nobody came. Leave the page as it is and report.
- **8**: quiet hours (02:00-08:00). Nothing was sent, nothing was waited for.
  **Skip this item and move to the next one.** Do not retry, and do not sleep
  and try again. That is the dead wait 8 exists to prevent.
- **1**: could not ask (usually `--mode` does not match the live session).

Pass the mode the session is **already** in. assist refuses a mismatch rather
than switching, because switching relaunches Chrome and would destroy the page
you want a human to look at.

Ask only when a human genuinely unblocks the task. Every request is logged with
its reason and its outcome, and a flow that needs a person on every run is a
flow that needs rethinking, not more pushes.

## Never submit without being told

Do not click anything that submits an application, sends a message, makes a
purchase, or posts publicly unless the task explicitly instructs it. Fill the
form, screenshot it, and report that it is ready. Submitting on your own
initiative is not being helpful. It is an irreversible act on someone's behalf.

## Uploads

Files must live under `var/uploads/` in the g-browser checkout. Anything else is
refused, which is what stops a service token being used to attach the passport
to a web form. The service resolves the path, so a relative one works from any
directory.

```sh
gbrowser upload --mode identity --ref e24 --chooser --file var/uploads/document.pdf
```

Use `--chooser` when the target is a styled button rather than a visible file
input. Most ATS forms are built this way. If a plain upload fails on a button
labelled "Attach", `--chooser` is the fix.

## Other things worth knowing

- `gbrowser status`: mode, egress, lock holder, bandwidth, fallback budget and
  per-job bytes.
- `gbrowser egress --mode identity`: probes the proxy and reports the live exit
  IP without launching the browser.
- `gbrowser run --steps -`: pipe a JSON step list for a long sequence.
- Jobs have a 150s budget by default (`--timeout` to raise). A job that overruns
  fails and releases the browser rather than wedging it.
- `--frugal strict` blocks images too. Fallback jobs enforce it automatically
  except for labelled screenshots, which allow images for that job.

## Report honestly

If a step failed, say so and include what the page looked like. Failures return
partial results plus a snapshot of where they stopped, so you always have that.
Do not describe a form as filled when a required field was left empty.
