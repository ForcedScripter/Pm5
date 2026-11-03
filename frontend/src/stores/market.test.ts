import { foldBar } from "./market";

test("1s bars fold into the current minute candle", () => {
  let c = foldBar([], [120, 10, 11, 9, 10.5]);
  c = foldBar(c, [121, 10.5, 12, 10, 11.5]);
  c = foldBar(c, [179, 11.5, 11.6, 8, 9]);
  expect(c).toEqual([{ time: 120, open: 10, high: 12, low: 8, close: 9 }]);
});

test("a new minute starts a new candle; late bars for older minutes are ignored", () => {
  let c = foldBar([{ time: 60, open: 1, high: 2, low: 1, close: 2 }], [125, 2, 3, 2, 3]);
  expect(c.map((x) => x.time)).toEqual([60, 120]);
  c = foldBar(c, [70, 9, 9, 9, 9]);
  expect(c.at(-1)!.close).toBe(3);
  expect(c[0].close).toBe(2);
});
