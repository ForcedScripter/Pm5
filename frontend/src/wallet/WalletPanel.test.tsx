import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WalletPanel } from "./WalletPanel";

const KEY = "0x" + "4c".repeat(32);
const FUNDER = "0x" + "ab".repeat(20);

function setup(fetchImpl: typeof fetch) {
  globalThis.fetch = fetchImpl;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><WalletPanel /></QueryClientProvider>);
}
const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));

test("link button stays disabled until key and address are complete", async () => {
  setup(vi.fn(() => json({ connected: false })) as never);
  const btn = await screen.findByRole("button", { name: "LINK WALLET" });
  expect(btn).toBeDisabled();
  fireEvent.change(screen.getByPlaceholderText(/64 hex/), { target: { value: KEY } });
  expect(btn).toBeDisabled();
  fireEvent.change(screen.getByPlaceholderText("0x…"), { target: { value: FUNDER } });
  expect(btn).toBeEnabled();
});

test("posts only to our own /api/wallet, never persists the key in browser storage", async () => {
  const calls: { url: string; body?: string }[] = [];
  let connected = false;
  setup(vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body as string });
    if (init?.method === "POST") { connected = true; return json({ connected: true, signer_address: "0x2c75", funder_address: FUNDER, signature_type: 1, source: "app", api_creds_ok: true }); }
    return json(connected ? { connected: true, signer_address: "0x2c75", funder_address: FUNDER, signature_type: 1, source: "app", api_creds_ok: true } : { connected: false });
  }) as never);
  fireEvent.change(await screen.findByPlaceholderText(/64 hex/), { target: { value: KEY } });
  fireEvent.change(screen.getByPlaceholderText("0x…"), { target: { value: FUNDER } });
  fireEvent.click(screen.getByRole("button", { name: "LINK WALLET" }));
  await waitFor(() => expect(screen.getByText(/Disconnect & delete key/)).toBeInTheDocument());
  const post = calls.find((c) => c.body);
  expect(post!.url).toBe("/api/wallet");
  expect(JSON.parse(post!.body!)).toEqual({ private_key: KEY, funder_address: FUNDER, signature_type: 1 });
  expect(calls.every((c) => c.url.startsWith("/api/"))).toBe(true);
  const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
  expect(stored).not.toContain(KEY.slice(2));
  expect(document.body.innerHTML).not.toContain(KEY.slice(2));   // wiped from the form after success
});

test("shows server error and keeps the input for correction", async () => {
  setup(vi.fn((_u: string, init?: RequestInit) =>
    init?.method === "POST" ? json({ detail: "Polymarket rejected these credentials: PolyApiException" }, 400) : json({ connected: false })) as never);
  fireEvent.change(await screen.findByPlaceholderText(/64 hex/), { target: { value: KEY } });
  fireEvent.change(screen.getByPlaceholderText("0x…"), { target: { value: FUNDER } });
  fireEvent.click(screen.getByRole("button", { name: "LINK WALLET" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Polymarket rejected");
  expect(screen.getByPlaceholderText(/64 hex/)).toHaveValue(KEY);
});
