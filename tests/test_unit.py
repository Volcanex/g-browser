"""Fast unit tests: pure logic, no browser or network."""
import importlib
import asyncio
import json
import os
from contextlib import closing

import pytest

from browser.proxy import RESIDENTIAL


def _reload_proxy():
    import browser.proxy as p
    return importlib.reload(p)


def test_proxy_none_without_creds(monkeypatch):
    for k in ("WEBSHARE_RESIDENTIAL_HOST", "WEBSHARE_RESIDENTIAL_USER",
              "WEBSHARE_RESIDENTIAL_PASS", "GBROWSER_PROXY_DISABLE", "GBROWSER_PROXY_SESSION"):
        monkeypatch.delenv(k, raising=False)
    assert _reload_proxy().build_proxy(RESIDENTIAL) is None


def test_proxy_uses_passport_username_verbatim(monkeypatch):
    monkeypatch.setenv("WEBSHARE_RESIDENTIAL_HOST", "p.webshare.io")
    monkeypatch.setenv("WEBSHARE_RESIDENTIAL_USER", "sub-gb-city_london-818660")
    monkeypatch.setenv("WEBSHARE_RESIDENTIAL_PASS", "secret")
    monkeypatch.delenv("GBROWSER_PROXY_DISABLE", raising=False)
    monkeypatch.delenv("GBROWSER_PROXY_SESSION", raising=False)
    p = _reload_proxy().build_proxy(RESIDENTIAL)
    assert p["server"] == "http://p.webshare.io:80"
    assert p["username"] == "sub-gb-city_london-818660"   # verbatim, not mangled


def test_proxy_session_override_swaps_only_last_segment(monkeypatch):
    monkeypatch.setenv("WEBSHARE_RESIDENTIAL_HOST", "p.webshare.io")
    monkeypatch.setenv("WEBSHARE_RESIDENTIAL_USER", "sub-gb-city_london-818660")
    monkeypatch.setenv("WEBSHARE_RESIDENTIAL_PASS", "secret")
    monkeypatch.setenv("GBROWSER_PROXY_SESSION", "rotate9")
    monkeypatch.delenv("GBROWSER_PROXY_DISABLE", raising=False)
    p = _reload_proxy().build_proxy(RESIDENTIAL)
    assert p["username"] == "sub-gb-city_london-rotate9"


def test_proxy_disable_flag(monkeypatch):
    monkeypatch.setenv("WEBSHARE_RESIDENTIAL_HOST", "p.webshare.io")
    monkeypatch.setenv("WEBSHARE_RESIDENTIAL_USER", "sub-gb-city_london-818660")
    monkeypatch.setenv("WEBSHARE_RESIDENTIAL_PASS", "secret")
    monkeypatch.setenv("GBROWSER_PROXY_DISABLE", "1")
    assert _reload_proxy().build_proxy(RESIDENTIAL) is None


def test_login_fails_closed_when_password_unset(monkeypatch):
    monkeypatch.delenv("GBROWSER_PASSWORD", raising=False)
    import auth
    importlib.reload(auth)

    class _Req:
        headers = {}
        class client: host = "1.2.3.4"
    class _Resp:
        def set_cookie(self, *a, **k): pass

    with pytest.raises(Exception):
        auth.do_login("", _Req(), _Resp())          # empty==empty must NOT authenticate
    with pytest.raises(Exception):
        auth.do_login("anything", _Req(), _Resp())


def test_admin_cookie_fails_closed_when_value_unset(monkeypatch):
    """The cookie value used to be the constant "ok", so anyone could forge
    it. Unset must never match, not even an empty or missing cookie."""
    monkeypatch.delenv("GBROWSER_COOKIE_VALUE", raising=False)
    import auth
    importlib.reload(auth)

    class _Req:
        def __init__(self, cookies): self.cookies = cookies

    assert not auth.is_admin(_Req({}))
    assert not auth.is_admin(_Req({auth.COOKIE: ""}))
    assert not auth.is_admin(_Req({auth.COOKIE: "ok"}))


def test_admin_cookie_matches_only_the_configured_value(monkeypatch):
    monkeypatch.setenv("GBROWSER_COOKIE_VALUE", "test-cookie-value")
    import auth
    importlib.reload(auth)

    class _Req:
        def __init__(self, cookies): self.cookies = cookies

    assert auth.is_admin(_Req({auth.COOKIE: "test-cookie-value"}))
    assert not auth.is_admin(_Req({auth.COOKIE: "ok"}))
    assert not auth.is_admin(_Req({auth.COOKIE: "tést"}))  # non-ASCII must not raise


def test_mode_must_be_named():
    """No default mode: a consumer that forgets to think about identity must be
    refused, not quietly handed the owner's profile."""
    from browser import modes
    with pytest.raises(ValueError):
        modes.resolve(None)
    with pytest.raises(ValueError):
        modes.resolve("nonsense")
    assert modes.resolve("research").identity is False
    assert modes.resolve("identity").identity is True
    assert modes.resolve("direct").profile == modes.RESEARCH.profile


def test_ref_parsing_is_strict():
    """A malformed ref must never be coerced into a plausible one."""
    from browser import snapshot
    assert snapshot.parse_ref("e12") == ("", 12)
    assert snapshot.parse_ref("f3e7") == ("f3", 7)
    for bad in ("", "e", "12", "button.submit", "f3e", "e12x", None):
        with pytest.raises(ValueError):
            snapshot.parse_ref(bad)


def test_upload_paths_are_confined(monkeypatch, tmp_path):
    """The passport must not be attachable to a web form."""
    import importlib
    monkeypatch.setenv("GBROWSER_UPLOAD_DIR", str(tmp_path))
    import browser.core as core
    importlib.reload(core)
    inside = tmp_path / "document.pdf"
    inside.write_text("cv")
    assert core._safe_upload(str(inside)) == str(inside.resolve())
    with pytest.raises(ValueError, match="upload refused"):
        core._safe_upload(str(tmp_path.parent / ".env"))
    with pytest.raises(ValueError):
        core._safe_upload(str(tmp_path / ".." / "escape.txt"))


# ---- assist ---------------------------------------------------------------

def _assist(tmp_path, monkeypatch):
    """assist with its state redirected into a tmp dir."""
    import assist
    monkeypatch.setattr(assist, "ASSIST_DIR", tmp_path)
    monkeypatch.setattr(assist, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(assist, "LOG", tmp_path / "log.jsonl")
    return assist


def _at(hour, minute=0):
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime(2026, 8, 4, hour, minute, tzinfo=ZoneInfo("Europe/London"))


def test_quiet_hours_window():
    a = __import__("assist")
    assert a.in_quiet_hours(_at(2, 0))
    assert a.in_quiet_hours(_at(4, 30))
    assert a.in_quiet_hours(_at(7, 59))
    assert not a.in_quiet_hours(_at(8, 0))       # 08:00 sharp is awake
    assert not a.in_quiet_hours(_at(1, 59))
    assert not a.in_quiet_hours(_at(23, 0))


def test_quiet_hours_are_the_owners_not_the_hosts():
    """h runs on UTC; the window must still be 02:00-08:00 in London, or it
    silently shifts by an hour every summer."""
    import datetime as dt
    a = __import__("assist")
    assert a.in_quiet_hours(dt.datetime(2026, 8, 4, 1, 30, tzinfo=dt.timezone.utc))   # 02:30 BST
    assert not a.in_quiet_hours(dt.datetime(2026, 8, 4, 7, 30, tzinfo=dt.timezone.utc))  # 08:30 BST


def test_quiet_hours_defer_without_pinging_or_waiting(tmp_path, monkeypatch):
    """Deferral must not touch the browser, the phone, or the clock. A queue
    that stops for two minutes per wall spends the night asleep."""
    a = _assist(tmp_path, monkeypatch)
    calls = []
    for name in ("push", "probe"):
        monkeypatch.setattr(a, name, lambda *args, **kw: calls.append(name))

    def boom(*args, **kw):
        raise AssertionError("assist touched the browser during quiet hours")

    code = a.request("Turnstile checkbox", "identity", 120, boom, boom,
                     out=lambda m: None, now=_at(3, 0), sleep=boom)
    assert code == a.EXIT_DEFERRED == 8
    assert calls == []
    assert json.loads((tmp_path / "log.jsonl").read_text())["outcome"] == "deferred"


def test_resolution_signals():
    a = __import__("assist")
    walled = {"challenge": {"kind": "cloudflare", "confidence": "high"}, "tokens": [],
              "url": "https://x/apply"}
    clear = {"challenge": None, "tokens": [], "url": "https://x/apply"}
    assert a.resolution(walled, clear) == "challenge cleared"
    assert a.resolution(walled, walled) is None

    # An embedded widget is invisible to detect(); the token is the only signal.
    form = {"challenge": None, "tokens": [], "url": "https://x/apply"}
    solved = {"challenge": None, "tokens": ["cf-turnstile-response"], "url": "https://x/apply"}
    assert a.resolution(form, solved) == "token: cf-turnstile-response"
    assert a.resolution(solved, solved) is None          # already filled != just solved

    assert a.resolution(form, {**form, "url": "https://x/thanks"}) == "navigated"
    assert a.resolution(form, {**form, "acked": True}) == "ack"

    # A passive reCAPTCHA badge was never a wall, so its disappearance is not a
    # resolution.
    passive = {"challenge": {"kind": "recaptcha", "confidence": "low"}, "tokens": [],
               "url": "https://x/apply"}
    assert a.resolution(passive, clear) is None


def test_ack_nonce_is_the_capability(tmp_path, monkeypatch):
    a = _assist(tmp_path, monkeypatch)
    nonce = a.open_pending("Turnstile checkbox", "https://x/apply", "identity")
    assert not a.is_acked(nonce)
    assert a.ack(nonce) is True
    assert a.is_acked(nonce)

    assert a.ack("f" * 32) is False                       # unknown nonce
    assert a.ack("../../etc/passwd") is False             # not a nonce at all
    assert a.ack("") is False

    a.close_pending(nonce)
    assert a.ack(nonce) is False                          # dead once the wait ends


def _flow(a, monkeypatch, probes, timeout=120, acked_after=None):
    """Drive request() on a fake clock: each poll advances POLL_S."""
    t = {"now": 0.0}
    seen = {"n": 0}
    monkeypatch.setattr(a, "push", lambda *args, **kw: {"ok": True})

    def fake_probe(post, mode):
        i = min(seen["n"], len(probes) - 1)
        seen["n"] += 1
        return dict(probes[i])

    monkeypatch.setattr(a, "probe", fake_probe)
    if acked_after is not None:
        monkeypatch.setattr(a, "is_acked", lambda n: seen["n"] > acked_after)
    else:
        monkeypatch.setattr(a, "is_acked", lambda n: False)

    out = []
    code = a.request("Turnstile checkbox", "identity", timeout,
                     get=lambda p: {"status": {"mode": "identity", "current_url": "https://x/apply"}},
                     post=None, out=out.append, now=_at(14, 0),
                     sleep=lambda s: t.__setitem__("now", t["now"] + s),
                     clock=lambda: t["now"])
    return code, out, t["now"]


def test_exit_zero_when_the_page_says_a_human_dealt_with_it(tmp_path, monkeypatch):
    a = _assist(tmp_path, monkeypatch)
    walled = {"challenge": {"kind": "cloudflare", "confidence": "high"}, "tokens": [],
              "url": "https://x/apply"}
    clear = {"challenge": None, "tokens": [], "url": "https://x/apply"}
    code, _, waited = _flow(a, monkeypatch, [walled, walled, clear])
    assert code == a.EXIT_OK == 0
    assert waited < 120
    row = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[-1])
    assert row["outcome"] == "resolved" and row["signal"] == "challenge cleared"


def test_exit_seven_when_nobody_comes(tmp_path, monkeypatch):
    a = _assist(tmp_path, monkeypatch)
    walled = {"challenge": {"kind": "cloudflare", "confidence": "high"}, "tokens": [],
              "url": "https://x/apply"}
    code, _, waited = _flow(a, monkeypatch, [walled], timeout=30)
    assert code == a.EXIT_TIMEOUT == 7
    assert waited >= 30
    row = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[-1])
    assert row["outcome"] == "timeout" and row["waited_s"] >= 30


def test_ack_ends_the_wait(tmp_path, monkeypatch):
    a = _assist(tmp_path, monkeypatch)
    walled = {"challenge": {"kind": "cloudflare", "confidence": "high"}, "tokens": [],
              "url": "https://x/apply"}
    code, _, _ = _flow(a, monkeypatch, [walled], acked_after=2)
    assert code == a.EXIT_OK
    assert json.loads((tmp_path / "log.jsonl").read_text().splitlines()[-1])["signal"] == "ack"


def test_mode_mismatch_is_refused_not_switched(tmp_path, monkeypatch):
    """Switching mode relaunches Chrome, which would destroy the page a human is
    being summoned to."""
    a = _assist(tmp_path, monkeypatch)
    monkeypatch.setattr(a, "push", lambda *args, **kw: pytest.fail("pushed anyway"))
    monkeypatch.setattr(a, "probe", lambda *args, **kw: pytest.fail("probed anyway"))
    code = a.request("Turnstile checkbox", "identity", 120,
                     get=lambda p: {"status": {"mode": "research", "current_url": "https://x"}},
                     post=None, out=lambda m: None, now=_at(14, 0), sleep=lambda s: None)
    assert code == a.EXIT_FAIL == 1
    assert json.loads((tmp_path / "log.jsonl").read_text())["outcome"] == "refused"


def test_no_push_means_no_wait(tmp_path, monkeypatch):
    """A wait nobody was told about is a dead wait."""
    a = _assist(tmp_path, monkeypatch)
    monkeypatch.setattr(a, "push", lambda *args, **kw: {"ok": False, "error": "NTFY_URL unset"})
    monkeypatch.setattr(a, "probe", lambda *args, **kw: {"challenge": None, "tokens": [],
                                                         "url": "https://x/apply"})
    code = a.request("Turnstile checkbox", "identity", 120,
                     get=lambda p: {"status": {"mode": "identity", "current_url": "https://x"}},
                     post=None, out=lambda m: None, now=_at(14, 0),
                     sleep=lambda s: pytest.fail("waited without notifying"))
    assert code == a.EXIT_FAIL
    assert json.loads((tmp_path / "log.jsonl").read_text())["outcome"] == "unnotified"


def test_push_headers_are_tappable_and_leak_nothing(monkeypatch):
    """Click/Actions are the point. A notification you cannot act on from the
    lock screen waits for a laptop. The service token must never be in one."""
    a = __import__("assist")
    sent = {}

    class _Req:
        def __init__(self, url, data=None, headers=None):
            sent.update(url=url, data=data, headers=headers or {})

    conf = {"NTFY_URL": "https://ntfy.example/topic", "GBROWSER_PANEL_URL": "https://panel.example/"}
    monkeypatch.setattr(a, "env", lambda k: conf.get(k, ""))
    monkeypatch.setattr(a.urllib.request, "Request", _Req)
    monkeypatch.setattr(a.urllib.request, "urlopen", lambda *args, **kw: _Ctx())
    assert a.push("Turnstile\ncheckbox \u2014 tick it", "https://x/apply", "identity", "a" * 32) == {"ok": True}

    h = sent["headers"]
    assert h["Click"] == "https://panel.example"
    assert f"https://panel.example/api/assist/ack/{'a' * 32}" in h["Actions"]
    blob = json.dumps(h) + sent["data"].decode()
    assert "GBROWSER_SERVICE_TOKEN" not in blob and "TOKEN=" not in blob
    for v in h.values():
        v.encode("ascii")                      # header injection / latin-1 crash
        assert "\n" not in v


def test_push_without_panel_url_still_notifies(monkeypatch):
    """The panel URL is per instance and has no default. Missing it costs the
    links, never the push: a human who knows beats a dead wait."""
    a = __import__("assist")
    sent = {}

    class _Req:
        def __init__(self, url, data=None, headers=None):
            sent.update(headers=headers or {})

    monkeypatch.setattr(a, "env", lambda k: "https://ntfy.example/topic" if k == "NTFY_URL" else "")
    monkeypatch.setattr(a.urllib.request, "Request", _Req)
    monkeypatch.setattr(a.urllib.request, "urlopen", lambda *args, **kw: _Ctx())
    r = a.push("Turnstile checkbox", "https://x/apply", "identity", "a" * 32)
    assert r["ok"] and "GBROWSER_PANEL_URL" in r["warning"]
    assert "Click" not in sent["headers"] and "Actions" not in sent["headers"]


class _Ctx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


# ---- fallback -------------------------------------------------------------

def _fallback(tmp_path, monkeypatch, limit=20):
    from browser.fallback import FallbackBudget
    monkeypatch.setenv("GBROWSER_FALLBACK_PER_HOUR", str(limit))
    return FallbackBudget(str(tmp_path / "fallback.sqlite3"))


def test_fallback_default_budget_refuses_the_21st(tmp_path, monkeypatch):
    from browser.fallback import FallbackBudget
    monkeypatch.delenv("GBROWSER_FALLBACK_PER_HOUR", raising=False)
    budget = FallbackBudget(str(tmp_path / "fallback.sqlite3"))
    for i in range(20):
        assert budget.reserve("research", f"caller-{i}")["ok"]
    refused = budget.reserve("direct", "another-caller")
    assert refused["ok"] is False
    assert refused["exit_code"] == 9
    assert "budget frees at" in refused["error"]
    assert 0 < refused["retry_after_s"] <= 3600
    assert budget.status()["used"] == 20


def test_fallback_window_rolls_and_expires_at_the_boundary(tmp_path, monkeypatch):
    import browser.fallback as fallback
    budget = _fallback(tmp_path, monkeypatch, 2)
    clock = [1000.0]
    monkeypatch.setattr(fallback.time, "time", lambda: clock[0])
    assert budget.reserve("research", "first")["ok"]
    clock[0] = 2800
    assert budget.reserve("research", "second")["ok"]
    clock[0] = 4599.5
    refused = budget.reserve("research", "third")
    assert refused["retry_at"] == 4600
    assert refused["retry_after_s"] == 1
    clock[0] = 4600
    assert budget.reserve("research", "third")["ok"]
    assert [j["holder"] for j in budget.status()["jobs"]] == ["second", "third"]


def test_fallback_restart_preserves_failed_jobs_and_bytes(tmp_path, monkeypatch):
    from browser.fallback import FallbackBudget
    budget = _fallback(tmp_path, monkeypatch, 1)
    admitted = budget.reserve("research", "test")
    budget.finish(admitted["job_id"], 1234, False)
    restarted = FallbackBudget(budget.path)
    assert restarted.reserve("research", "test")["exit_code"] == 9
    status = restarted.status()
    assert status["remaining"] == 0
    assert status["jobs"][0]["bytes"] == 1234
    assert status["jobs"][0]["ok"] == 0


def test_fallback_lowered_limit_reports_when_enough_jobs_expire(tmp_path, monkeypatch):
    import browser.fallback as fallback
    budget = _fallback(tmp_path, monkeypatch, 2)
    monkeypatch.setattr(fallback.time, "time", lambda: 1000)
    budget.reserve("research", "first")
    monkeypatch.setattr(fallback.time, "time", lambda: 1100)
    budget.reserve("research", "second")
    smaller = _fallback(tmp_path, monkeypatch, 1)
    assert smaller.reserve("research", "third")["retry_at"] == 4700


@pytest.mark.parametrize("limit", ["0", "-1", "invalid"])
def test_fallback_requires_a_positive_integer(tmp_path, monkeypatch, limit):
    from browser.fallback import FallbackBudget
    monkeypatch.setenv("GBROWSER_FALLBACK_PER_HOUR", limit)
    with pytest.raises(ValueError):
        FallbackBudget(str(tmp_path / "fallback.sqlite3"))


def test_fallback_admission_is_atomic_across_callers(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    budget = _fallback(tmp_path, monkeypatch, 2)
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda i: budget.reserve("research", str(i)), range(8)))
    assert sum(result["ok"] for result in results) == 2
    assert sum(result.get("exit_code") == 9 for result in results) == 6
    assert budget.status()["used"] == 2


def _fallback_manager(tmp_path, monkeypatch):
    import browser.core as core
    monkeypatch.setattr(core, "VAR", str(tmp_path))
    monkeypatch.setenv("GBROWSER_FALLBACK_PER_HOUR", "1")
    manager = core.BrowserManager()
    manager.mode = core.modes.RESEARCH
    manager.attached = True
    from types import SimpleNamespace
    class Page:
        url = "about:blank"
        closed = False
        def is_closed(self): return self.closed
        async def close(self): self.closed = True
        async def wait_for_timeout(self, ms): await asyncio.sleep(ms / 1000)

    async def new_tab(page=None):
        tab = core.Tab(page or Page(), SimpleNamespace())
        manager._tabs[tab.id] = tab
        manager._page = tab.page
        return tab

    monkeypatch.setattr(manager, "_new_tab", new_tab)
    return manager


def test_fallback_refuses_before_waiting_for_the_browser_lock(tmp_path, monkeypatch):
    manager = _fallback_manager(tmp_path, monkeypatch)
    manager.fallback.reserve("research", "test")

    async def check():
        async with manager._lock:
            result = await asyncio.wait_for(manager.run(
                {"mode": "research", "fallback": True, "action": "snapshot"}), timeout=0.1)
        assert result["exit_code"] == 9
        assert manager.holder is None

    asyncio.run(check())


@pytest.mark.parametrize("mode,flag", [("identity", True), ("research", False)])
def test_fallback_exempts_identity_and_unflagged_legacy_jobs(tmp_path, monkeypatch, mode, flag):
    from browser import modes
    manager = _fallback_manager(tmp_path, monkeypatch)
    manager.mode = modes.resolve(mode)
    monkeypatch.setattr(manager.fallback, "reserve", lambda *a: pytest.fail("budget charged"))

    async def run_steps(*args):
        return {"ok": True}

    monkeypatch.setattr(manager, "_run_steps", run_steps)
    assert asyncio.run(manager.run({"mode": mode, "fallback": flag, "action": "snapshot"}))["ok"]
    assert manager.fallback.status()["used"] == 0


@pytest.mark.parametrize("mode", ["research", "direct"])
@pytest.mark.parametrize("labelled", [False, True])
def test_fallback_enforces_blocking_and_records_job_bytes(tmp_path, monkeypatch, mode, labelled):
    from types import SimpleNamespace
    from browser import core, modes
    manager = _fallback_manager(tmp_path, monkeypatch)
    manager.mode = modes.resolve(mode)
    async def action(step, tab):
        for kind in ("Image", "Media", "Font", "Document"):
            if core._blocked("https://example.com/file", kind, "https://example.com",
                             False, tab.frugal, tab.images):
                blocked.append(kind.lower())
        manager._request(tab, {"requestId": "1", "request": {"url": "https://example.com"}, "type": "Document"})
        manager._finished(tab, {"requestId": "1", "encodedDataLength": 321})
        return {"done": True}

    async def detect(page):
        return None

    blocked = []
    monkeypatch.setattr(manager, "_do", action)
    monkeypatch.setattr(core.snapshot, "detect", detect)
    steps = [{"action": "screenshot", "label_refs": labelled}]
    result = asyncio.run(manager.run({"mode": mode, "fallback": True,
                                     "frugal": False, "steps": steps}))
    assert result["ok"]
    assert blocked == (["media", "font"] if labelled else ["image", "media", "font"])
    assert result["bytes"] == 321
    assert manager.status()["last_job"]["bytes"] == 321
    assert manager.status()["fallback"]["jobs"][0]["bytes"] == 321
    assert all(t.job is None for t in manager._tabs.values())


def test_fallback_failed_browser_start_still_counts(tmp_path, monkeypatch):
    manager = _fallback_manager(tmp_path, monkeypatch)
    manager.attached = False
    result = asyncio.run(manager.run({"mode": "research", "fallback": True, "action": "snapshot"}))
    assert not result["ok"]
    assert result["bytes"] == 0
    assert manager.fallback.status()["used"] == 1
    assert manager.fallback.status()["jobs"][0]["ok"] == 0


@pytest.mark.parametrize("flag", [False, True])
def test_cli_research_warning_and_fallback_payload(monkeypatch, capsys, flag):
    import cli
    jobs = []
    monkeypatch.setattr(cli, "post", lambda path, job, **kw: jobs.append(job) or {"ok": True})
    code = cli.main(["snapshot", "--mode", "research"] + (["--fallback"] if flag else []))
    assert code == 0
    assert jobs[0].get("fallback", False) == flag
    assert capsys.readouterr().err == ("" if flag else
        "the research browser is retired for agents: use g-scraper; --fallback for the hardest walls\n")


def test_cli_preserves_budget_exit_code_from_http_429(monkeypatch, capsys):
    import io
    import cli
    from urllib.error import HTTPError
    result = {"ok": False, "exit_code": 9, "error": "fallback budget exhausted; budget frees at 12:00 UTC"}

    def refuse(*args, **kwargs):
        raise HTTPError("http://example.com/run", 429, "Too Many Requests", {},
                        io.BytesIO(json.dumps(result).encode()))

    monkeypatch.setattr(cli, "token", lambda: "test")
    monkeypatch.setattr(cli.urllib.request, "urlopen", refuse)
    assert cli.main(["snapshot", "--mode", "research", "--fallback"]) == 9
    assert "budget frees at" in capsys.readouterr().err


def test_fallback_api_field_and_retry_header(tmp_path, monkeypatch):
    import auth
    from fastapi.testclient import TestClient
    monkeypatch.setattr(auth, "COOKIE_VALUE", "test-cookie")
    monkeypatch.setenv("GBROWSER_SERVICE_TOKEN", "test-token")
    server = importlib.import_module("server")
    manager = _fallback_manager(tmp_path, monkeypatch)
    manager.attached = False
    monkeypatch.setattr(server, "manager", manager)
    # Do not enter TestClient itself: that would launch real Chrome.
    with closing(TestClient(server.app)) as client:
        headers = {"X-Gbrowser-Token": "test-token"}
        job = {"mode": "research", "fallback": True, "action": "snapshot"}
        assert client.post("/run", json=job, headers=headers).status_code == 200
        refused = client.post("/run", json=job, headers=headers)
        assert refused.status_code == 429
        assert refused.json()["exit_code"] == 9
        assert int(refused.headers["Retry-After"]) > 0


@pytest.mark.parametrize("cap", ["0", "-2", "bad"])
def test_tab_cap_requires_positive_integer(tmp_path, monkeypatch, cap):
    import browser.core as core
    monkeypatch.setenv("GBROWSER_MAX_TABS", cap)
    with pytest.raises(ValueError):
        core.BrowserManager()


def test_parallel_tabs_are_capped_and_mode_switch_drains(tmp_path, monkeypatch):
    from browser import core, modes
    manager = _fallback_manager(tmp_path, monkeypatch)
    manager.mode = modes.IDENTITY
    started = []
    release = asyncio.Event()

    async def steps(*args):
        tab = args[-1]
        started.append((manager.mode.name, tab.id))
        if manager.mode.identity:
            await release.wait()
        return {"ok": True}

    async def relaunch(name):
        assert not manager._busy()
        assert len(started) == 3
        manager._tabs.clear()
        manager.mode = modes.resolve(name)
        return {"ok": True}

    monkeypatch.setattr(manager, "_run_steps", steps)
    monkeypatch.setattr(manager, "_relaunch", relaunch)

    async def check():
        jobs = [asyncio.create_task(manager.run({"mode": "identity", "url": "about:blank"}, str(i)))
                for i in range(3)]
        for _ in range(20):
            await asyncio.sleep(0)
        assert manager.status()["tabs"]["busy"] == 3
        switch = asyncio.create_task(manager.run({"mode": "research", "action": "snapshot"}, "switch"))
        fourth = asyncio.create_task(manager.run({"mode": "identity", "url": "about:blank"}, "fourth"))
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(started) == 3
        assert not switch.done() and not fourth.done()
        release.set()
        results = await asyncio.gather(*jobs, switch)
        fourth.cancel()
        await asyncio.gather(fourth, return_exceptions=True)
        assert all(r["ok"] for r in results)
        assert len({r["tab_id"] for r in results[:3]}) == 3
        assert started[3][0] == "research"
        assert manager.holder is None
        await manager.stop()

    asyncio.run(check())


def test_explicit_tab_and_legacy_followups_keep_refs_and_owner(tmp_path, monkeypatch):
    manager = _fallback_manager(tmp_path, monkeypatch)

    async def steps(*args):
        tab = args[-1]
        tab.sigs.setdefault("e1", tab.id)
        assert tab.sigs["e1"] == tab.id
        return {"ok": True}

    monkeypatch.setattr(manager, "_run_steps", steps)

    async def check():
        first = await manager.run({"mode": "research", "url": "about:blank"}, "one")
        legacy = await manager.run({"mode": "research", "action": "snapshot"}, "one")
        assert legacy["tab_id"] == first["tab_id"]
        other = await manager.run({"mode": "research", "url": "about:blank"}, "two")
        assert other["tab_id"] != first["tab_id"]
        follow = await manager.run({"mode": "research", "tab_id": first["tab_id"]}, "one")
        assert follow["tab_id"] == first["tab_id"]
        refused = await manager.run({"mode": "research", "tab_id": first["tab_id"]}, "two")
        assert not refused["ok"] and "tab_id" in refused["error"]
        await manager._close_tab(manager._tabs[first["tab_id"]])
        expired = await manager.run({"mode": "research", "tab_id": first["tab_id"]}, "one")
        assert not expired["ok"]
        await manager.stop()

    asyncio.run(check())


def test_per_job_bytes_exclude_cache_and_other_tabs(tmp_path, monkeypatch):
    manager = _fallback_manager(tmp_path, monkeypatch)

    async def steps(*args):
        tab = args[-1]
        n = int(tab.holder)
        for request_id, cached in (("network", False), ("cache", True)):
            manager._request(tab, {"requestId": request_id, "request": {"url": "https://example.com/a"}, "type": "Script"})
            manager._response(tab, {"requestId": request_id, "response": {"fromDiskCache": cached}})
            if not cached:
                manager._data(tab, {"requestId": request_id, "encodedDataLength": n})
            await asyncio.sleep(0)
            manager._finished(tab, {"requestId": request_id, "encodedDataLength": n + 10})
        return {"ok": True}

    monkeypatch.setattr(manager, "_run_steps", steps)

    async def check():
        results = await asyncio.gather(*(manager.run({"mode": "research", "url": "about:blank"}, str(n))
                                         for n in (100, 200, 300)))
        assert [r["bytes"] for r in results] == [110, 210, 310]
        assert manager.bandwidth()["research"]["total"] == 630
        assert all(r["bandwidth"]["by_resource_type"] == {"Script": r["bytes"]} for r in results)
        await manager.stop()

    asyncio.run(check())


def test_timeout_and_cancellation_release_tabs(tmp_path, monkeypatch):
    from browser import core
    manager = _fallback_manager(tmp_path, monkeypatch)

    async def steps(*args):
        await asyncio.sleep(10)

    async def detect(page): return None
    monkeypatch.setattr(manager, "_run_steps", steps)
    monkeypatch.setattr(core.snapshot, "detect", detect)

    async def check():
        result = await manager.run({"mode": "research", "timeout_s": .01, "snapshot_on_error": False})
        assert not result["ok"] and "budget" in result["error"]
        assert not manager.status()["busy"]
        task = asyncio.create_task(manager.run({"mode": "research"}))
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not manager.status()["busy"]

    asyncio.run(check())


def test_idle_extra_tabs_close_but_one_remains(tmp_path, monkeypatch):
    from browser import core
    manager = _fallback_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(core, "IDLE_TAB_SECONDS", .01)

    async def steps(*args):
        await asyncio.sleep(.01)
        return {"ok": True}
    monkeypatch.setattr(manager, "_run_steps", steps)

    async def check():
        await asyncio.gather(*(manager.run({"mode": "research", "url": "about:blank"}, str(i)) for i in range(3)))
        await asyncio.sleep(.04)
        assert manager.status()["tabs"]["total"] == 1
        await manager.stop()

    asyncio.run(check())


@pytest.mark.parametrize("url,kind,images,blocked", [
    ("https://www.googletagmanager.com/gtm.js", "Script", False, True),
    ("https://www.example.com/main.js", "Script", False, False),
    ("https://www.example.com/beacon", "Ping", False, True),
    ("https://cdn.example.com/a.jpg", "Image", False, True),
    ("https://cdn.example.com/a.jpg", "Image", True, False),
    ("https://cdn.example.com/a.mp4", "Media", True, True),
    ("https://cdn.example.com/a.woff", "Font", True, True),
    ("https://challenges.cloudflare.com/a", "Script", False, False),
    ("https://www.google.com/recaptcha/api.js", "Script", False, False),
    ("https://www.gstatic.com/recaptcha/image", "Image", False, False),
    ("https://geo.captcha-delivery.com/image", "Image", False, False),
    ("https://js.hcaptcha.com/a", "Script", False, False),
    ("https://www.example.com/cdn-cgi/challenge-platform/image", "Image", False, False),
])
def test_identity_filter_preserves_content_and_challenges(url, kind, images, blocked):
    from browser.core import _blocked
    assert _blocked(url, kind, "https://www.example.com", True, False, images) == blocked


def test_cli_tab_and_image_options_use_existing_api_params(monkeypatch):
    import cli
    jobs = []
    monkeypatch.setattr(cli, "post", lambda path, job, **kw: jobs.append(job) or {"ok": True})
    assert cli.main(["snapshot", "--mode", "identity", "--tab", "abc", "--images"]) == 0
    assert jobs[0]["params"] == {"tab_id": "abc", "images": True}


def test_assist_observes_the_only_tab_without_changing_its_owner(tmp_path, monkeypatch):
    manager = _fallback_manager(tmp_path, monkeypatch)

    async def steps(*args): return {"ok": True}
    monkeypatch.setattr(manager, "_run_steps", steps)

    async def check():
        first = await manager.run({"mode": "research", "url": "about:blank"}, "cli")
        assist = await manager.run({"mode": "research", "action": "snapshot"}, "assist")
        assert assist["tab_id"] == first["tab_id"]
        assert manager._tabs[first["tab_id"]].holder == "cli"
        follow = await manager.run({"mode": "research", "action": "snapshot"}, "cli")
        assert follow["tab_id"] == first["tab_id"]

    asyncio.run(check())


def test_cdp_auth_answers_only_proxy_challenges_and_bounds_retries(tmp_path, monkeypatch):
    from browser import core
    from types import SimpleNamespace
    manager = _fallback_manager(tmp_path, monkeypatch)
    manager._proxy = {"username": "test-user", "password": "test-secret"}
    sent = []

    async def send(method, params):
        sent.append(params["authChallengeResponse"])

    tab = core.Tab(None, SimpleNamespace(send=send))

    async def check():
        proxy = {"requestId": "1", "authChallenge": {"source": "Proxy"}}
        await manager._authenticate(tab, proxy)
        await manager._authenticate(tab, proxy)
        await manager._authenticate(tab, {"requestId": "2", "authChallenge": {"source": "Server"}})

    asyncio.run(check())
    assert sent == [{"response": "ProvideCredentials", "username": "test-user", "password": "test-secret"},
                    {"response": "CancelAuth"}, {"response": "Default"}]
