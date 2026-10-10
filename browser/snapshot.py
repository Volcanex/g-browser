"""Page perception: what the hand reports back to the brain.

gbrowser's job API used to speak only CSS selectors, which meant a consumer had
to already know the page. That works for a consumer that already holds
hard-won choreography for its site, and fails completely for an agent meeting a
multi-step web form for the first time. Its only discovery tool was `read_html`,
which returns 300KB of React noise.

This module is the alternative: walk the live DOM in every frame, keep only what
a human would actually perceive, and hand back a numbered outline:

    [e11] button   "Easy Apply"
    [e12] textbox  "Years of experience"  (required, empty)

The agent then acts on `ref: "e12"`. No selectors, no DOM.

Three things here are load-bearing and not obvious from the code:

- **Refs must go stale loudly.** Every ref carries a signature (role + name)
  captured at snapshot time. Resolving re-checks `isConnected` AND the
  signature, because React happily reuses a DOM node for a different button.
  A stale ref silently clicking the wrong control on a real form is
  the worst thing this system can do, so resolution fails closed.

- **Frames are namespaced, not flattened.** ATS apply forms (Greenhouse, Ashby,
  Workday, LinkedIn's modal) live in iframes; a snapshot that stops at the main
  document sees none of the form. Refs are prefixed per frame (`f1e3`).

- **The ref map lives in the page, not here.** `window.__gb.refs` dies with the
  JS context on navigation, which is exactly the invalidation semantics we
  want: after a nav, every old ref reports stale for free.

Challenge detection lives here too because it is perception, not policy: this
module reports "you are looking at a Cloudflare interstitial", and leaves what
to do about it to the caller.
"""

from __future__ import annotations

import asyncio
import os
import re

SNAPSHOT_JS_VERSION = 4

# Injected wholesale on every snapshot; it self-detects an existing install and
# reuses the ref map so ids stay stable across snapshots of the same page.
_JS = r"""
(() => {
  const V = %d;
  if (window.__gb && window.__gb.V === V) return window.__gb;

  const refs = new Map();      // id -> Element
  const ids  = new WeakMap();  // Element -> id
  let counter = 0;

  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','HEAD','META','LINK','TITLE','SVG','PATH','TEMPLATE','BR','HR']);
  const INTERACTIVE = new Set(['link','button','textbox','searchbox','spinbutton','checkbox','radio','file',
                               'combobox','listbox','slider','switch','tab','menuitem','menuitemcheckbox',
                               'menuitemradio','date','color','option']);
  // Roles that genuinely end a subtree. Everything else keeps walking, however
  // interactive it looks.
  const LEAF_ROLES = new Set(['link','button','textbox','searchbox','spinbutton',
                              'checkbox','radio','file','combobox','slider','switch',
                              'option','date','color','menuitem','tab']);
  const INPUT_ROLE = {text:'textbox',email:'textbox',password:'textbox',tel:'textbox',url:'textbox',
                      search:'searchbox',number:'spinbutton',checkbox:'checkbox',radio:'radio',file:'file',
                      submit:'button',button:'button',reset:'button',image:'button',date:'date',
                      'datetime-local':'date',month:'date',week:'date',time:'date',range:'slider',color:'color',
                      hidden:null};

  const squash = (s, max) => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return s.length > max ? s.slice(0, max - 1) + '…' : s;
  };

  // Two different questions, and conflating them cost us the whole of LinkedIn.
  // `hidden` means the subtree is genuinely not rendered and can be skipped.
  // `paintable` means this particular element is worth emitting. A wrapper with
  // height:0 is not paintable but its children very much are, so pruning on size
  // deleted the entire job view and left four messaging-overlay buttons.
  function hidden(el) {
    if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return true;
    let s;
    try { s = getComputedStyle(el); } catch (e) { return true; }
    return !s || s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0';
  }

  function paintable(el) {
    const r = el.getBoundingClientRect();
    // Off-viewport still counts: scrollable content is real.
    return r.width > 1 || r.height > 1;
  }

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.trim().split(/\s+/)[0];
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      return (t in INPUT_ROLE) ? INPUT_ROLE[t] : 'textbox';
    }
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'img') return 'image';
    if (tag === 'iframe' || tag === 'frame') return 'iframe';
    if (tag === 'label') return 'label';
    return null;
  }

  function directText(el) {
    let s = '';
    for (const n of el.childNodes) if (n.nodeType === 3) s += n.nodeValue;
    return squash(s, 300);
  }

  function accName(el) {
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return squash(al, 120);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const parts = lb.split(/\s+/)
        .map(id => { const n = el.ownerDocument.getElementById(id); return n ? squash(n.innerText || n.textContent, 80) : ''; })
        .filter(Boolean);
      if (parts.length) return squash(parts.join(' '), 120);
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      try {
        if (el.labels && el.labels.length) {
          const t = Array.from(el.labels).map(l => squash(l.innerText || l.textContent, 80)).filter(Boolean).join(' ');
          if (t) return squash(t, 120);
        }
      } catch (e) {}
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (type === 'submit' || type === 'button' || type === 'reset') {
        const v = squash(el.value, 120);
        if (v) return v;
      }
      for (const a of ['placeholder', 'title', 'aria-placeholder', 'name']) {
        const v = el.getAttribute(a);
        if (v && v.trim()) return squash(v, 120);
      }
      return '';
    }
    if (tag === 'img') return squash(el.getAttribute('alt'), 120);
    if (tag === 'iframe' || tag === 'frame') return squash(el.getAttribute('title') || el.getAttribute('name'), 120);
    const t = squash(el.innerText || el.textContent, 120);
    if (t) return t;
    return squash(el.getAttribute('title'), 120);
  }

  function statesOf(el) {
    const out = [];
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (el.required || el.getAttribute('aria-required') === 'true') out.push('required');
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') out.push('disabled');
    if (type === 'checkbox' || type === 'radio' || el.getAttribute('role') === 'checkbox') {
      const c = el.checked !== undefined ? el.checked : el.getAttribute('aria-checked') === 'true';
      out.push(c ? 'checked' : 'unchecked');
    }
    const ex = el.getAttribute('aria-expanded');
    if (ex !== null) out.push('expanded=' + ex);
    if (tag === 'select') {
      out.push((el.options ? el.options.length : 0) + ' options');
      const sel = el.selectedIndex >= 0 && el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '';
      if (sel) out.push('selected="' + squash(sel, 40) + '"');
    } else if (type === 'file') {
      out.push(el.files && el.files.length ? el.files.length + ' file(s) attached' : 'no file');
    } else if (tag === 'input' || tag === 'textarea') {
      if (!['checkbox', 'radio', 'submit', 'button', 'reset', 'image'].includes(type)) {
        // Never surface a password back to a consumer, even truncated.
        const v = type === 'password' ? (el.value ? '••••' : '') : squash(el.value, 40);
        out.push(v ? 'value="' + v + '"' : 'empty');
      }
    }
    if (tag === 'a') {
      const href = el.getAttribute('href') || '';
      if (/^https?:/i.test(href)) {
        try { out.push(new URL(href, location.href).hostname); } catch (e) {}
      }
    }
    return out;
  }

  function interactive(el) {
    const r = roleOf(el);
    if (r && INTERACTIVE.has(r)) return true;
    if (el.hasAttribute('onclick')) return true;
    if (el.isContentEditable) return true;
    const ti = el.getAttribute('tabindex');
    if (ti !== null && ti !== '-1') return true;
    return false;
  }

  const sigOf = (el) => (roleOf(el) || el.tagName.toLowerCase()) + '|' + accName(el).slice(0, 60);

  function refFor(el) {
    let id = ids.get(el);
    if (id === undefined) { id = ++counter; ids.set(el, id); refs.set(id, el); }
    return id;
  }

  // Depth-first in reading order, descending into shadow roots. `out` collects
  // flat records; the caller renders them.
  function walk(node, out, depth, budget) {
    const kids = node.children ? Array.from(node.children) : [];
    for (const el of kids) {
      if (out.length >= budget.max) { budget.truncated = true; return; }
      if (SKIP.has(el.tagName)) continue;
      if (hidden(el)) continue;

      const role = roleOf(el);
      const shown = paintable(el);
      let bumped = false;

      if (!shown) {
        // Not worth emitting, but its children may be.
        if (el.shadowRoot) walk(el.shadowRoot, out, depth, budget);
        walk(el, out, depth, budget);
        continue;
      }

      if (interactive(el)) {
        const id = refFor(el);
        out.push({ ref: id, role: role || 'clickable', name: accName(el), states: statesOf(el),
                   depth, sig: sigOf(el) });
        bumped = true;
      } else if (role === 'heading') {
        out.push({ role: el.tagName.toLowerCase(), name: accName(el), states: [], depth });
        bumped = true;
      } else if (role === 'iframe') {
        out.push({ role: 'iframe', name: accName(el) || squash(el.getAttribute('src'), 90), states: [], depth });
        bumped = true;
      } else {
        const t = directText(el);
        if (t.length > 1) { out.push({ role: 'text', name: t, states: [], depth }); bumped = true; }
      }

      if (el.shadowRoot) walk(el.shadowRoot, out, depth + 1, budget);
      // Real controls are leaves: their inner spans and icons are noise. But
      // "interactive" also catches any element carrying tabindex or onclick, and
      // those are routinely scroll wrappers containing the entire page. Treating
      // those as leaves pruned the whole of LinkedIn's job view down to four
      // elements, so only genuine controls stop the walk.
      const isLeafControl = role && LEAF_ROLES.has(role);
      if (role !== 'heading' && !isLeafControl) walk(el, out, depth + (bumped ? 1 : 0), budget);
    }
  }

  function collect(max, withinSelector) {
    let root = document.body || document.documentElement;
    if (withinSelector) {
      const r = document.querySelector(withinSelector);
      if (r) root = r;
    }
    const budget = { max: max, truncated: false };
    const out = [];
    walk(root, out, 0, budget);
    return { items: out, truncated: budget.truncated, total: refs.size };
  }

  function resolve(id) {
    const el = refs.get(id);
    return (el && el.isConnected) ? el : null;
  }

  function overlay(on, prefix) {
    const ID = '__gb_overlay';
    const prev = document.getElementById(ID);
    if (prev) prev.remove();
    if (!on) return 0;
    const box = document.createElement('div');
    box.id = ID;
    box.setAttribute('style', 'all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;');
    let n = 0;
    for (const [id, el] of refs) {
      if (!el.isConnected) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue;
      const ring = document.createElement('div');
      ring.setAttribute('style', `all:initial;position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;` +
        `height:${r.height}px;border:1px solid rgba(255,214,0,.9);box-sizing:border-box;pointer-events:none;`);
      box.appendChild(ring);
      const tag = document.createElement('div');
      tag.textContent = prefix + 'e' + id;
      tag.setAttribute('style', `all:initial;position:fixed;left:${Math.max(0, r.left)}px;` +
        `top:${Math.max(0, r.top - 11)}px;background:#ffd600;color:#000;` +
        `font:700 9px/11px ui-monospace,SFMono-Regular,Menlo,monospace;padding:0 2px;` +
        `border:1px solid #000;pointer-events:none;white-space:nowrap;`);
      box.appendChild(tag);
      n++;
    }
    document.documentElement.appendChild(box);
    return n;
  }

  // ---- challenge detection ------------------------------------------------
  // Perception only: report what wall this looks like and how sure we are.
  // Confidence matters: a passive reCAPTCHA badge on a login form is not a
  // block, and treating it as one would stop the agent for no reason.
  function detect() {
    const hits = [];
    const q = (s) => { try { return document.querySelector(s); } catch (e) { return null; } };
    const body = document.body ? squash(document.body.innerText, 4000) : '';
    const title = document.title || '';
    const href = location.href;

    if (q('#challenge-running') || q('#cf-challenge-running') || q('[class*="cf-browser-verification"]') ||
        q('#cf-please-wait') || /^just a moment/i.test(title))
      hits.push({ kind: 'cloudflare', confidence: 'high', evidence: 'cloudflare challenge markers' });
    else if (q('script[src*="challenge-platform"]') && /checking your browser|verify you are human|review the security of your connection/i.test(body))
      hits.push({ kind: 'cloudflare', confidence: 'high', evidence: 'cloudflare interstitial text' });

    if (q('iframe[src*="captcha-delivery.com"]') || q('[id*="datadome"]') || q('.datadome-captcha') ||
        /dd_cookie|datadome/i.test(title))
      hits.push({ kind: 'datadome', confidence: 'high', evidence: 'datadome captcha frame' });

    if (q('iframe[src*="hcaptcha.com"][src*="frame=challenge"]') || q('.h-captcha'))
      hits.push({ kind: 'hcaptcha', confidence: 'high', evidence: 'hcaptcha widget' });

    if (q('iframe[src*="recaptcha/api2/bframe"]') || q('iframe[src*="recaptcha/enterprise/bframe"]'))
      hits.push({ kind: 'recaptcha', confidence: 'high', evidence: 'recaptcha challenge frame' });
    else if (q('.g-recaptcha') || q('iframe[src*="recaptcha"]'))
      hits.push({ kind: 'recaptcha', confidence: 'low', evidence: 'recaptcha present, may be passive' });

    if (q('#px-captcha') || q('[id^="px-captcha"]'))
      hits.push({ kind: 'perimeterx', confidence: 'high', evidence: 'perimeterx captcha' });

    if (q('#px-block') || /pardon the interruption/i.test(body))
      hits.push({ kind: 'perimeterx', confidence: 'medium', evidence: 'perimeterx block page' });

    if (/\/authwall|\/checkpoint\/challenge|\/uas\/login/i.test(href) || /sign in to (view|see)/i.test(body))
      hits.push({ kind: 'login', confidence: 'high', evidence: 'auth wall, sign-in required' });

    // Where these words appear matters as much as whether they appear. A block
    // page puts them in its title or its one heading and carries almost nothing
    // else; ordinary prose uses the same words in passing. A LinkedIn advert
    // for a backend engineer asking for "rate limiting" tripped this on
    // 2026-08-04, was read as a wall, and halted a run that was working, so
    // body text now only counts when there is barely any of it.
    const blocked = /unusual (traffic|activity)|are you a robot|verify (that )?you are (a )?human|you have been blocked|access denied|too many requests|rate limit/i;
    const heads = Array.from(document.querySelectorAll('h1, h2'))
      .map((h) => h.textContent || '').join(' ').slice(0, 500);
    if (blocked.test(title) || blocked.test(heads) || (body.length < 1200 && blocked.test(body)))
      hits.push({ kind: 'generic', confidence: 'medium', evidence: 'block-page wording' });

    if (!hits.length) return null;
    const rank = { high: 3, medium: 2, low: 1 };
    hits.sort((a, b) => rank[b.confidence] - rank[a.confidence]);
    return { kind: hits[0].kind, confidence: hits[0].confidence, evidence: hits[0].evidence, all: hits };
  }

  window.__gb = { V, refs, collect, resolve, overlay, detect, sigOf, accName, roleOf };
  return window.__gb;
})()
""" % SNAPSHOT_JS_VERSION

# Frames that are pure ad/analytics furniture. Snapshotting them is noise and
# occasionally an enormous amount of it.
_JUNK_FRAME = re.compile(
    r"doubleclick\.net|googlesyndication|googletagmanager|google-analytics|"
    r"facebook\.com/(tr|plugins)|adservice\.|scorecardresearch|hotjar|"
    r"/recaptcha/|/gtm\.|criteo|taboola|outbrain",
    re.I,
)

_MAIN = ""          # ref prefix for the top-level document


def frame_prefix(index: int) -> str:
    """Ref namespace for a frame: main document is bare (`e7`), others `f1e7`."""
    return _MAIN if index == 0 else f"f{index}"


_REF_RE = re.compile(r"^(f\d+)?e(\d+)$")


def parse_ref(ref: str):
    """`'f2e13'` -> `('f2', 13)`. Raises ValueError on anything else, because a
    malformed ref must never be coerced into a plausible one."""
    m = _REF_RE.match((ref or "").strip())
    if not m:
        raise ValueError(f"malformed ref {ref!r}, expected e12 or f1e12")
    return (m.group(1) or _MAIN), int(m.group(2))


def _render(sections, truncated_frames) -> str:
    """Flat records -> the outline an agent reads."""
    lines = []
    for prefix, url, items in sections:
        label = "main frame" if prefix == _MAIN else f"frame {prefix}"
        lines.append(f"### {label}: {url}")
        for it in items:
            indent = "  " * min(it.get("depth", 0), 6)
            name = it.get("name") or ""
            states = it.get("states") or []
            suffix = f"  ({', '.join(states)})" if states else ""
            if "ref" in it:
                tag = f"[{prefix}e{it['ref']}]"
                lines.append(f'{tag:<9} {indent}{it["role"]:<9} "{name}"{suffix}')
            elif it["role"] == "text":
                lines.append(f'{"":<9} {indent}{name}')
            else:
                lines.append(f'{"":<9} {indent}{it["role"]:<9} "{name}"')
        if prefix in truncated_frames:
            lines.append(f'{"":<9} … truncated: element budget reached for this frame')
        lines.append("")
    return "\n".join(lines).rstrip()


async def _usable_frames(page):
    """(prefix, frame) for every frame worth walking, main document first."""
    out = []
    idx = 0
    for f in page.frames:
        if f.is_detached():
            continue
        url = f.url or ""
        is_main = f is page.main_frame
        if not is_main:
            if _JUNK_FRAME.search(url):
                continue
            if url in ("", "about:blank"):
                continue
        idx_for_this = 0 if is_main else (idx + 1)
        if not is_main:
            idx += 1
        out.append((frame_prefix(idx_for_this), f))
    return out


# Every page-side call is wrapped in a wall-clock timeout. `frame.evaluate` has
# no timeout of its own and blocks indefinitely when a page keeps re-navigating
# under it, which is exactly what a Cloudflare interstitial does. Without this
# a single bot wall wedges the shared browser forever.
EVAL_TIMEOUT = float(os.environ.get("GBROWSER_EVAL_TIMEOUT", "20"))


async def _eval(frame, script, arg=None, timeout=None):
    coro = frame.evaluate(script, arg) if arg is not None else frame.evaluate(script)
    return await asyncio.wait_for(coro, timeout=timeout or EVAL_TIMEOUT)


async def install(frame):
    """Inject (or reuse) the perception library in one frame."""
    await _eval(frame, _JS)


async def take(page, max_elements: int = 400, within: str | None = None) -> dict:
    """Snapshot every usable frame.

    Returns the rendered outline plus the ref->signature map the caller needs to
    validate refs later, and whatever challenge was detected on the way past.
    """
    sections = []
    signatures = {}
    frames = {}
    truncated = set()
    total_refs = 0

    for prefix, frame in await _usable_frames(page):
        try:
            await install(frame)
            data = await _eval(
                frame,
                "([max, within]) => window.__gb.collect(max, within)",
                [max_elements, within if prefix == _MAIN else None],
            )
        except (Exception, asyncio.TimeoutError) as exc:
            # A frame that navigated mid-walk, or a cross-origin frame we can't
            # reach, is reported rather than swallowed. A silently missing
            # frame is a silently missing form.
            sections.append((prefix, frame.url, [{"role": "text", "name": f"[frame unreadable: {exc}]", "depth": 0}]))
            continue

        items = data.get("items", [])
        for it in items:
            if "ref" in it:
                signatures[f"{prefix}e{it['ref']}"] = it.get("sig", "")
                total_refs += 1
        if data.get("truncated"):
            truncated.add(prefix)
        frames[prefix] = frame.url
        sections.append((prefix, frame.url, items))

    return {
        "snapshot": _render(sections, truncated),
        "refs": total_refs,
        "truncated": bool(truncated),
        "frames": frames,
        "url": page.url,
        "challenge": await detect(page),
        # Consumed by BrowserManager to validate refs on later actions, and
        # stripped before the payload goes back over the wire. It is bookkeeping,
        # not something an agent should have to read.
        "signatures": signatures,
    }


async def detect(page) -> dict | None:
    """Bot-wall / auth-wall perception for the main document.

    Returns None when the page looks ordinary. Never decides anything: the
    caller (and eventually the governor) owns the response.
    """
    try:
        await install(page.main_frame)
        return await _eval(page.main_frame, "() => window.__gb.detect()")
    except (Exception, asyncio.TimeoutError):
        return None


class StaleRef(Exception):
    """The ref no longer points at what it pointed at when it was handed out."""


async def resolve(page, ref: str, frames: dict, signatures: dict):
    """Ref -> live ElementHandle, or raise StaleRef.

    Fails closed at every step. The signature re-check is the one that matters:
    an SPA can keep a node connected while repurposing it, and we would
    otherwise click the wrong control with full confidence.
    """
    prefix, num = parse_ref(ref)

    frame = None
    for p, f in await _usable_frames(page):
        if p == prefix:
            frame = f
            break
    if frame is None:
        raise StaleRef(f"{ref}: frame {prefix or 'main'} is gone, re-snapshot")

    try:
        handle = await asyncio.wait_for(
            frame.evaluate_handle(
                "(id) => (window.__gb && window.__gb.V === %d) ? window.__gb.resolve(id) : null"
                % SNAPSHOT_JS_VERSION,
                num,
            ),
            timeout=EVAL_TIMEOUT,
        )
    except (Exception, asyncio.TimeoutError) as exc:
        raise StaleRef(f"{ref}: frame not reachable ({exc}), re-snapshot")

    el = handle.as_element()
    if el is None:
        raise StaleRef(f"{ref}: not in the ref map: the page navigated or the element was removed. Re-snapshot.")

    expected = signatures.get(ref)
    if expected is not None:
        try:
            actual = await asyncio.wait_for(
                el.evaluate("(e) => window.__gb.sigOf(e)"), timeout=EVAL_TIMEOUT
            )
        except (Exception, asyncio.TimeoutError) as exc:
            raise StaleRef(f"{ref}: element detached ({exc}), re-snapshot")
        if actual != expected:
            raise StaleRef(
                f"{ref}: element changed identity (was {expected!r}, now {actual!r}), re-snapshot before acting"
            )
    return el


async def set_overlay(page, on: bool) -> int:
    """Draw (or clear) ref badges in every frame's own document.

    Drawing inside each frame rather than compositing coordinates in the parent
    is what makes this correct at any nesting depth. The browser does the
    compositing for us when the screenshot is taken.
    """
    n = 0
    for prefix, frame in await _usable_frames(page):
        try:
            await install(frame)
            n += await _eval(frame, "([on, p]) => window.__gb.overlay(on, p)", [on, prefix])
        except (Exception, asyncio.TimeoutError):
            continue
    return n
