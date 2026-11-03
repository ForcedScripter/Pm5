import asyncio


async def test_backtest_api_runs_and_reports(client, auth):
    r = await client.post("/api/backtests", headers=auth, json={"strategies": ["custom_btc", "constellation"], "days": 30})
    assert r.status_code == 202, r.text
    bid = r.json()["id"]
    for _ in range(100):
        d = (await client.get(f"/api/backtests/{bid}", headers=auth)).json()
        if d["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.1)
    # the test DB holds a few synthetic rounds; either it ran, or it explains why not, never silently
    assert d["status"] in ("done", "failed")
    if d["status"] == "done":
        assert "overall" in d["results"] and "split" in d["results"] and isinstance(d["results"]["warnings"], list)
    else:
        assert d["error"]
    assert any(b["id"] == bid for b in (await client.get("/api/backtests", headers=auth)).json())
    assert (await client.delete(f"/api/backtests/{bid}", headers=auth)).json()["ok"]


async def test_backtest_validation_and_other_endpoints(client, auth):
    assert (await client.post("/api/backtests", headers=auth, json={"strategies": ["nope"]})).status_code == 422
    assert (await client.post("/api/backtests", headers=auth, json={"strategies": []})).status_code == 422
    cov = (await client.get("/api/data/coverage", headers=auth)).json()
    assert {"settled_rounds", "recorded_rounds", "backfilled_rounds", "spot_bars"} <= set(cov)
    a = (await client.get("/api/analytics?mode=paper", headers=auth)).json()
    assert a["mode"] == "paper" and "overall" in a
    assert (await client.post("/api/backfill", headers=auth, json={"days": 100})).status_code == 422
