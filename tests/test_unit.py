"""Fast unit tests: pure logic, no browser or network."""
import importlib
import json
import os

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
