// End-to-end smoke test: login → every page renders with live data → save a strategy → verify via API.
// Usage: ADMIN_PASSWORD=... node e2e/smoke.cjs [baseUrl] [screenshotDir]
const { chromium } = require("playwright");
const base = process.argv[2] || "http://127.0.0.1:5173";
const out = process.argv[3] || "../docs/screenshots";
const pw = process.env.ADMIN_PASSWORD;
const proxy = process.env.HTTPS_PROXY ? { server: process.env.HTTPS_PROXY, bypass: "127.0.0.1,localhost" } : undefined;
const fail = (m) => { console.error("FAIL:", m); process.exitCode = 1; };

(async () => {
  const b = await chromium.launch({ proxy });
  for (const vp of [{ name: "desktop", width: 1440, height: 1000 }, { name: "mobile", width: 390, height: 844 }]) {
    const ctx = await b.newContext({ viewport: vp, ignoreHTTPSErrors: !!proxy });
    const p = await ctx.newPage();
    const errors = [];
    p.on("pageerror", (e) => errors.push(String(e)));
    p.on("console", (m) => m.type() === "error" && !/favicon|fonts\.g/.test(m.text()) && errors.push(m.text()));

    await p.goto(base + "/login");
    await p.fill('input[type="password"]', pw);
    await p.click('button:has-text("Sign in")');
    await p.waitForURL(base + "/");
    // live data must actually arrive
    await p.waitForFunction(() => /\$\d/.test(document.body.innerText) && !document.body.innerText.includes("Loading BTC candles"), null, { timeout: 30000 })
      .catch(() => fail(`${vp.name}: terminal never showed live prices/candles`));
    await p.waitForSelector("text=Order book");
    await p.waitForFunction(() => document.body.innerText.includes("spread"), null, { timeout: 20000 }).catch(() => fail(`${vp.name}: no order book`));
    await p.waitForTimeout(1500);
    await p.screenshot({ path: `${out}/terminal-${vp.name}.png`, fullPage: vp.name === "desktop" });
    const overflow = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (overflow > 1) fail(`${vp.name}: horizontal overflow ${overflow}px`);

    for (const [path, marker] of [["/settings", "Strategy configuration"], ["/history", "Trade history"], ["/backtest", "History available"], ["/logs", "Execution log"], ["/signals", "The six coins"]]) {
      await p.goto(base + path);
      await p.waitForSelector(`text=${marker}`, { timeout: 15000 }).catch(() => fail(`${vp.name}: ${path} missing "${marker}"`));
      await p.waitForTimeout(path === "/signals" ? 4000 : 1200);
      await p.screenshot({ path: `${out}${path}-${vp.name}.png`, fullPage: vp.name === "desktop" });
      const o = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      if (o > 1) fail(`${vp.name}: ${path} horizontal overflow ${o}px`);
    }

    if (vp.name === "desktop") {
      // functional check: change Constellation "Min assets agreeing" and save
      await p.goto(base + "/settings");
      await p.waitForSelector("text=Min assets agreeing");
      const token = await p.evaluate(() => JSON.parse(localStorage.getItem("pm5-auth")).state.token);
      const before = await (await p.request.get(base + "/api/strategies/constellation", { headers: { Authorization: `Bearer ${token}` } })).json();
      const input = p.locator('label:has-text("Min assets agreeing")').locator("xpath=..").locator("input");
      await input.fill(String(before.params.min_consensus === 4 ? 5 : 4));
      await p.click('button:has-text("Save strategy")');
      await p.waitForSelector(`text=v${before.version + 1} saved`, { timeout: 8000 }).catch(() => fail("save confirmation not shown"));
      const after = await (await p.request.get(base + "/api/strategies/constellation", { headers: { Authorization: `Bearer ${token}` } })).json();
      if (after.version !== before.version + 1 || after.params.min_consensus === before.params.min_consensus) fail("strategy save did not persist");
      else console.log(`strategy save ok: v${before.version} → v${after.version}, min_consensus ${before.params.min_consensus} → ${after.params.min_consensus}`);
      // bot control is live and reflects server state
      const bot = p.locator('header button:has-text("bot")');
      if (!(await bot.count())) fail("no Start/Stop bot control in header");
      await p.goto(base + "/signals");
      await p.waitForSelector("text=Recent signals", { timeout: 10000 }).catch(() => fail("signals history panel missing"));
      await p.goto(base + "/settings");
      await p.waitForSelector("text=Calls taken", { timeout: 10000 }).catch(() => fail("strategy scorecard missing"));
      // live gate must refuse without paper record
      const en = p.locator('button:has-text("Enable live trading")');
      if (!(await en.isDisabled())) fail("live enable button should be disabled before acceptance criteria pass");
    }
    if (errors.length) fail(`${vp.name} console errors:\n  ` + errors.slice(0, 8).join("\n  "));
    await ctx.close();
  }
  await b.close();
  console.log(process.exitCode ? "E2E FAILED" : "E2E PASSED");
})();
