import { useInfiniteQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Empty, Panel, PanelHeader } from "@/components/ui/panel";
import { cn } from "@/lib/utils";
import { Api } from "@/services/api";

const FILTERS = [
  { key: "all", label: "All" }, { key: "trades", label: "Trades" }, { key: "signals", label: "Signals" },
  { key: "skips", label: "Skips" }, { key: "errors", label: "Errors" }, { key: "system", label: "System" },
];

export default function Logs() {
  const [cat, setCat] = useState("all");
  const q = useInfiniteQuery({
    queryKey: ["logs-page", cat],
    queryFn: ({ pageParam }) => Api.logs(cat, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => (last.length >= 100 ? last.at(-1)!.id : undefined),
    refetchInterval: 5000,
  });
  const rows = q.data?.pages.flat() ?? [];
  return (
    <Panel className="mx-auto max-w-5xl">
      <PanelHeader icon={<ScrollText />} title="Execution log"
        right={<div className="flex gap-1" role="tablist">{FILTERS.map((f) => (
          <button key={f.key} role="tab" aria-selected={cat === f.key} onClick={() => setCat(f.key)}
            className={cn("rounded-full px-3 py-1 font-num text-[11px] font-bold tracking-wider uppercase", cat === f.key ? "gold-pill" : "text-tx-mid hover:text-tx-hi")}>{f.label}</button>
        ))}</div>} />
      {q.error ? <Empty>{q.error.message}</Empty> : rows.length === 0 ? (
        <Empty>{q.isLoading ? "Loading…" : "Nothing logged in this category yet. Every signal, skip (with its reason), risk check, order and settlement is written here once the engines run."}</Empty>
      ) : (
        <div className="divide-y divide-line">
          {rows.map((l) => (
            <div key={l.id} className="grid grid-cols-[150px_70px_1fr] gap-3 px-5 py-2 text-[12.5px]">
              <span className="num text-tx-dim">{new Date(l.ts).toLocaleString()}</span>
              <span className={cn("font-num text-[11px] font-bold uppercase", l.level === "error" ? "text-down" : l.level === "warn" ? "text-warn" : "text-azure")}>{l.category}</span>
              <span className={cn(l.level === "error" ? "text-down" : "text-tx")}>{l.message}</span>
            </div>
          ))}
          {q.hasNextPage && <div className="p-4 text-center"><Button size="sm" onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Load older</Button></div>}
        </div>
      )}
    </Panel>
  );
}
