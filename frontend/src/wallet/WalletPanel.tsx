import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, KeyRound, ShieldCheck, Unplug, Wallet } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input, Segmented } from "@/components/ui/form";
import { Badge, Panel, PanelHeader } from "@/components/ui/panel";
import { Api } from "@/services/api";

const SIG_TYPES = [
  { value: "1", label: "Email / Magic", sub: "Polymarket email login" },
  { value: "2", label: "Browser wallet", sub: "MetaMask etc." },
  { value: "0", label: "Plain EOA", sub: "no proxy wallet" },
] as const;

export function WalletPanel() {
  const qc = useQueryClient();
  const wallet = useQuery({ queryKey: ["wallet"], queryFn: Api.wallet });
  const [key, setKey] = useState("");
  const [funder, setFunder] = useState("");
  const [sig, setSig] = useState<"0" | "1" | "2">("1");
  const [show, setShow] = useState(false);

  const connect = useMutation({
    mutationFn: () => Api.connectWallet({ private_key: key, funder_address: sig === "0" ? null : funder || null, signature_type: Number(sig) }),
    // on success the key is wiped from component state; on error it stays so a typo can be fixed
    onSuccess: () => { setKey(""); setShow(false); qc.invalidateQueries({ queryKey: ["wallet"] }); qc.invalidateQueries({ queryKey: ["readiness"] }); },
  });
  const disconnect = useMutation({
    mutationFn: Api.disconnectWallet,
    onSuccess: () => { qc.invalidateQueries(); },
  });

  const w = wallet.data;
  return (
    <Panel id="wallet" gold>
      <PanelHeader icon={<Wallet />} title="Wallet"
        right={w?.connected ? <Badge className="border-up/50 text-up">connected · {w.source === "env" ? ".env" : "saved"}</Badge> : <Badge className="border-line-hi text-tx-low">not connected</Badge>} />
      {w?.connected ? (
        <div className="space-y-4 p-5">
          <dl className="space-y-3 text-[13px]">
            <div><dt className="panel-title !text-[10.5px]">Signer address</dt><dd className="num mt-1 break-all text-tx-hi">{w.signer_address}</dd></div>
            {w.funder_address && <div><dt className="panel-title !text-[10.5px]">Polymarket (funder) address</dt><dd className="num mt-1 break-all text-tx-hi">{w.funder_address}</dd></div>}
            <div className="flex gap-6">
              <div><dt className="panel-title !text-[10.5px]">Signature type</dt><dd className="mt-1 text-tx">{SIG_TYPES.find((s) => s.value === String(w.signature_type))?.label}</dd></div>
              <div><dt className="panel-title !text-[10.5px]">Polymarket API creds</dt><dd className={w.api_creds_ok ? "mt-1 text-up" : "mt-1 text-warn"}>{w.api_creds_ok ? "verified" : w.source === "env" ? "derived at start-up" : "not verified"}</dd></div>
            </div>
          </dl>
          {w.source === "app" ? (
            <Button variant="danger" onClick={() => { if (confirm("Delete the saved key from this server and switch live trading off?")) disconnect.mutate(); }} loading={disconnect.isPending}>
              <Unplug className="h-4 w-4" />Disconnect & delete key
            </Button>
          ) : <p className="text-xs text-tx-low">This wallet comes from <code>PRIVATE_KEY</code> in the server's .env. Remove it there to disconnect, or save a wallet here to override it.</p>}
        </div>
      ) : (
        <form className="space-y-4 p-5" onSubmit={(e) => { e.preventDefault(); connect.mutate(); }} autoComplete="off">
          <div className="space-y-2">
            <span className="panel-title !text-[10.5px]">Account type</span>
            <Segmented value={sig} onChange={setSig} options={SIG_TYPES.map((s) => ({ ...s }))} />
          </div>
          <label className="block space-y-1.5">
            <span className="flex items-center justify-between"><span className="panel-title !text-[10.5px]">Private key</span>
              <a href="https://reveal.magic.link/polymarket" target="_blank" rel="noreferrer" className="flex items-center gap-1 rounded-full border border-amber/50 px-2.5 py-1 font-num text-[10.5px] font-bold text-amber hover:bg-amber/10">
                <KeyRound className="h-3 w-3" />REVEAL MAGIC KEY ↗</a></span>
            <div className="relative">
              <Input type={show ? "text" : "password"} value={key} onChange={(e) => setKey(e.target.value.trim())} placeholder="0x… (64 hex characters)"
                spellCheck={false} autoComplete="new-password" className="num pr-10" />
              <button type="button" onClick={() => setShow((v) => !v)} className="absolute top-1/2 right-3 -translate-y-1/2 text-tx-low hover:text-tx" aria-label={show ? "hide key" : "show key"}>
                {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}</button>
            </div>
          </label>
          {sig !== "0" && (
            <label className="block space-y-1.5">
              <span className="panel-title !text-[10.5px]">Polymarket address (profile / deposit)</span>
              <Input value={funder} onChange={(e) => setFunder(e.target.value.trim())} placeholder="0x…" spellCheck={false} className="num" />
            </label>
          )}
          {connect.error && <p className="text-sm text-down" role="alert">{connect.error.message}</p>}
          <Button variant="gold" size="lg" className="w-full" loading={connect.isPending} disabled={key.length < 64 || (sig !== "0" && funder.length !== 42)}>LINK WALLET</Button>
          <div className="flex gap-2 text-[11.5px] leading-relaxed text-tx-low">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-up" />
            <p>The key is sent only to <b className="text-tx">this server</b>, encrypted with your MASTER_KEY before storage, never shown again and never sent to any third party.
              Use a dedicated trading wallet, and only over HTTPS or localhost.</p>
          </div>
        </form>
      )}
    </Panel>
  );
}
