"""Verify tab-local refs and three overlapping identity reads on the live service."""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli


async def run(job, holder):
    return await asyncio.to_thread(cli.post, "/run", job, holder)


async def main():
    # Only interact with synthetic about:blank forms; external pages are read-only.
    async def form(name):
        result = await run({"mode": "identity", "url": "about:blank", "steps": [
            {"action": "eval", "script": f'document.body.innerHTML=\'<label for=x>{name}</label><input id=x>\''},
            {"action": "wait", "ms": 300}, {"action": "snapshot"}]}, "smoke-tabs-refs")
        assert result["ok"], result
        ref = re.search(r"\[(e\d+)\].*textbox", result["results"][-1]["snapshot"]).group(1)
        return result["tab_id"], ref

    tabs = await asyncio.gather(form("First"), form("Second"))
    assert tabs[0][0] != tabs[1][0]
    for i, (tab_id, ref) in enumerate(tabs):
        result = await run({"mode": "identity", "params": {"tab_id": tab_id}, "steps": [
            {"action": "fill", "ref": ref, "value": str(i)},
            {"action": "eval", "script": "document.querySelector('input').value"}]}, "smoke-tabs-refs")
        assert result["ok"] and result["results"][-1]["value"] == str(i), result
    for i, (tab_id, _) in enumerate(tabs):
        result = await run({"mode": "identity", "params": {"tab_id": tab_id},
                            "steps": [{"action": "eval", "script": "document.querySelector('input').value"}]},
                           "smoke-tabs-refs")
        assert result["results"][0]["value"] == str(i), result
    ambiguous = await run({"mode": "identity", "action": "snapshot"}, "smoke-tabs-refs")
    assert not ambiguous["ok"] and "multiple tabs" in ambiguous["error"], ambiguous
    print("PASS refs: two independent forms survive interleaved follow-up calls; ambiguous calls fail closed")

    async def read(i):
        return await run({"mode": "identity", "url": "https://api.ipify.org", "steps": [
            {"action": "wait", "ms": 5000}, {"action": "read_text"}]}, f"smoke-tabs-ip-{i}")

    tasks = [asyncio.create_task(read(i)) for i in range(3)]
    peak = 0
    while not all(t.done() for t in tasks):
        status = await asyncio.to_thread(cli.get, "/health")
        peak = max(peak, status["status"]["tabs"]["busy"])
        await asyncio.sleep(.25)
    results = await asyncio.gather(*tasks)
    for result in results:
        assert result["ok"], result
        ip = result["results"][-1]["text"].strip()
        assert ip == "195.40.133.26", ip
        print(json.dumps({k: result[k] for k in ("tab_id", "started_at", "finished_at", "bytes")} | {"exit_ip": ip}))
    overlap = min(r["finished_at"] for r in results) - max(r["started_at"] for r in results)
    assert overlap > 0 and peak == 3, (overlap, peak)
    print(f"PASS concurrency: peak {peak}/3 tabs busy; common overlap {overlap:.3f}s; one static IP")


if __name__ == "__main__":
    asyncio.run(main())
