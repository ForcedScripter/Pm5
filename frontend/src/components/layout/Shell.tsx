import * as Dialog from "@radix-ui/react-dialog";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Heart, LogOut, OctagonX, Wallet } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/panel";
import { ASSETS, ASSET_META, cn, fmtPct, fmtPrice } from "@/lib/utils";
import { Api } from "@/services/api";
import { useAuth } from "@/stores/auth";
import { useMarket } from "@/stores/market";

const NAV = [
  { to: "/", label: "Terminal" },
  { to: "/settings", label: "Settings" },
  { to: "/logs", label: "Logs" },
  { to: "/signals", label: "Signals" },
];

export function useNow(ms = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(t); }, [ms]);
  return now;
}

function Ticker() {
  const spot = useMarket((s) => s.spot);
  const items = ASSETS.map((a) => (
    <span key={a} className="flex items-center gap-2 px-5 whitespace-nowrap">
      <span style={{ color: ASSET_META[a].color }} className="text-sm">{ASSET_META[a].glyph}</span>
      <span className="font-num text-xs font-bold text-tx-hi">{ASSET_META[a].sym}</span>
      <span className="num text-xs text-tx">${fmtPrice(spot[a].price)}</span>
      <span className={cn("num text-xs font-semibold", (spot[a].change24h ?? 0) >= 0 ? "text-up" : "text-down")}>
        {(spot[a].change24h ?? 0) >= 0 ? "▲" : "▼"} {fmtPct(spot[a].change24h).replace(/^[+−]/, "")}
      </span>
    </span>
  ));
  return (
    <div className="overflow-hidden border-b border-line bg-bg-0/70 py-2" aria-label="price ticker">
      <div className="animate-ticker flex w-max">{items}{items}</div>
    </div>
  );
}

function FeedStatus() {
  const { status, lastTick } = useMarket();
  const now = useNow();
  const age = lastTick ? Math.round((now - lastTick) / 1000) : null;
  const ok = status === "live";
  return (
    <span className="hidden items-center gap-2 rounded-full border border-line-mid bg-bg-0/60 px-3 py-1.5 text-[11px] lg:flex" title="Binance market feed">
      <Heart className={cn("h-3.5 w-3.5", ok ? "fill-up text-up" : status === "stale" ? "fill-warn text-warn" : "text-down")} />
      <span className="text-tx">Market feed</span>
      <span className={cn("font-semibold", ok ? "text-amber" : "text-down")}>
        {status === "connecting" ? "connecting…" : age == null ? status : `last tick ${age}s ago`}
      </span>
    </span>
  );
}

function EmergencyStop() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const stop = useMutation({ mutationFn: Api.emergencyStop, onSuccess: () => { qc.invalidateQueries(); setOpen(false); } });
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <Button variant="danger" size="sm" className="h-9" aria-label="Emergency stop"><OctagonX className="h-4 w-4" /><span className="hidden xl:inline">STOP</span></Button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
        <Dialog.Content className="glass fixed top-1/2 left-1/2 z-50 w-[min(92vw,440px)] -translate-x-1/2 -translate-y-1/2 p-6">
          <Dialog.Title className="font-brand text-xl text-tx-hi">Emergency stop</Dialog.Title>
          <Dialog.Description className="mt-2 text-sm leading-relaxed text-tx">
            Turns the bot and live trading off immediately and blocks new orders until you clear it in Settings.
          </Dialog.Description>
          {stop.error && <p className="mt-3 text-sm text-down">{String(stop.error.message)}</p>}
          <div className="mt-5 flex justify-end gap-2">
            <Dialog.Close asChild><Button variant="ghost">Cancel</Button></Dialog.Close>
            <Button variant="danger" loading={stop.isPending} onClick={() => stop.mutate()}>Stop everything</Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Header() {
  const logout = useAuth((s) => s.logout);
  const nav = useNavigate();
  const wallet = useQuery({ queryKey: ["wallet"], queryFn: Api.wallet });
  const settings = useQuery({ queryKey: ["settings"], queryFn: Api.settings, refetchInterval: 10_000 });
  const s = settings.data;
  const mode = s?.emergency_stopped ? "STOPPED" : s?.live_trading_enabled ? "live mode" : "paper mode";
  const addr = wallet.data?.connected ? (wallet.data.funder_address ?? wallet.data.signer_address) : null;
  return (
    <header className="flex flex-wrap items-center gap-x-4 gap-y-3 border-b border-line px-4 py-3 lg:px-6">
      <Link to="/" className="flex items-center gap-3">
        <img src="/logo.svg" alt="" className="h-11 w-11 rounded-xl" />
        <div className="leading-none">
          <span className="font-brand text-xl font-semibold text-amber">pm5bot</span>
          <span className="ml-2 font-num text-[10px] font-bold tracking-[0.2em] text-azure">5-MIN BOT</span>
        </div>
        <Badge className={cn("hidden sm:inline-flex", s?.emergency_stopped ? "border-down/60 text-down" : s?.live_trading_enabled ? "border-up/60 text-up" : "border-amber/50 text-amber-hi")}>
          v0.3 · {mode}
        </Badge>
      </Link>
      <nav className="order-last flex w-full justify-center md:order-none md:w-auto md:flex-1" aria-label="main">
        <div className="flex gap-1 rounded-full border border-line-mid bg-bg-0/50 p-1">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"}
              className={({ isActive }) => cn("relative rounded-full px-4 py-2 font-num text-xs font-semibold tracking-[0.12em] uppercase transition-colors sm:px-5",
                isActive ? "text-[#2a1a06]" : "text-tx-mid hover:text-tx-hi")}>
              {({ isActive }) => (<>
                {isActive && <motion.span layoutId="navpill" className="gold-pill absolute inset-0 rounded-full" transition={{ type: "spring", stiffness: 420, damping: 34 }} />}
                <span className="relative">{n.label}</span>
              </>)}
            </NavLink>
          ))}
        </div>
      </nav>
      <div className="ml-auto flex items-center gap-2">
        <FeedStatus />
        <Button variant={addr ? "outline" : "wallet"} size="sm" className="h-9" onClick={() => nav("/settings#wallet")}>
          <Wallet className="h-4 w-4" />{addr ? `${addr.slice(0, 6)}…${addr.slice(-4)}` : "Link Wallet"}
        </Button>
        <EmergencyStop />
        <Button variant="ghost" size="icon" aria-label="Log out" onClick={logout}><LogOut className="h-4 w-4" /></Button>
      </div>
    </header>
  );
}

function Footer() {
  const { status } = useMarket();
  return (
    <footer className="mt-6 flex flex-wrap items-center justify-between gap-2 border-t border-line bg-bg-0/50 px-4 py-2.5 text-[11px] text-tx-low lg:px-6">
      <span className="flex items-center gap-2"><Heart className={cn("h-3 w-3", status === "live" ? "fill-up text-up" : "text-down")} />
        Prices from Binance · markets & books from Polymarket</span>
      <span className="font-num font-semibold tracking-wider text-tx-mid">EXPERIMENTAL · NOT FINANCIAL ADVICE</span>
    </footer>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const start = useMarket((s) => s.start);
  useEffect(() => start(), [start]);
  return (
    <div className="stars flex min-h-screen flex-col">
      <Ticker />
      <Header />
      <main className="mx-auto w-full max-w-[1600px] flex-1 px-4 pt-5 lg:px-6">{children}</main>
      <Footer />
    </div>
  );
}
