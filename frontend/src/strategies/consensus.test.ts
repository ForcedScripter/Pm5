import { btcRegime, evaluate } from "./consensus";

const prices = { xrp: { up: 0.52, down: 0.49 }, btc: { up: 0.7, down: 0.3 } };

test("UP consensus with a flat laggard in band passes all gates", () => {
  const r = evaluate({ btc: 0.08, eth: 0.07, sol: 0.09, doge: 0.06, bnb: 0.05, xrp: 0.002 }, prices, 100);
  expect(r.up.count).toBe(5);
  expect(r.up.met).toBe(true);
  expect(r.side).toBe("UP");
  const x = r.candidates.find((c) => c.asset === "xrp")!;
  expect(x.gates).toEqual({ consensus: true, laggard: true, band: true, timing: true });
  expect(x.pass).toBe(true);
});

test("count met but average below threshold fails consensus", () => {
  const r = evaluate({ btc: 0.02, eth: 0.02, sol: 0.03, doge: 0.02, bnb: 0.02, xrp: 0 }, prices, 100);
  expect(r.up.count).toBe(5);
  expect(r.up.met).toBe(false);
  expect(r.candidates[0].pass).toBe(false);
});

test("DOWN needs the larger 0.10 threshold", () => {
  const r = evaluate({ btc: -0.12, eth: -0.11, sol: -0.13, doge: -0.1, bnb: 0.0, xrp: -0.12 }, { bnb: { up: 0.5, down: 0.5 } }, 120);
  expect(r.down.met).toBe(true);
  expect(r.candidates.map((c) => c.asset)).toEqual(["bnb"]);
  expect(r.candidates[0].pass).toBe(true);
});

test("price outside band or outside timing window blocks entry", () => {
  const d = { btc: 0.08, eth: 0.07, sol: 0.09, doge: 0.06, bnb: 0.05, xrp: 0.0 };
  expect(evaluate(d, { xrp: { up: 0.8, down: 0.2 } }, 100).candidates[0].gates.band).toBe(false);
  expect(evaluate(d, prices, 20).phase).toBe("WARMUP");
  expect(evaluate(d, prices, 250).candidates[0].pass).toBe(false);
});

test("a coin already moving with the group is not a laggard", () => {
  const r = evaluate({ btc: 0.08, eth: 0.07, sol: 0.09, doge: 0.06, xrp: 0.004, bnb: 0.009 }, prices, 100);
  expect(r.up.count).toBe(4);
  expect(r.candidates.every((c) => c.gates.laggard)).toBe(true);
});

test("BTC regime thresholds", () => {
  expect(btcRegime(2.5)).toMatchObject({ state: "OVERHEATED", downThreshold: 0.06 });
  expect(btcRegime(-2.1)).toMatchObject({ state: "OVERSOLD", upThreshold: 0.03 });
  expect(btcRegime(0.4).state).toBe("NORMAL");
  expect(btcRegime(null).state).toBe("UNKNOWN");
});
