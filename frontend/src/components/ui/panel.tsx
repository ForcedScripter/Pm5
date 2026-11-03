import type * as React from "react";
import { cn } from "@/lib/utils";

export function Panel({ className, gold, ...p }: React.HTMLAttributes<HTMLDivElement> & { gold?: boolean }) {
  return <section className={cn("glass overflow-hidden", gold && "glass-gold", className)} {...p} />;
}

export function PanelHeader({ icon, title, right, className }: {
  icon?: React.ReactNode; title: React.ReactNode; right?: React.ReactNode; className?: string;
}) {
  return (
    <header className={cn("flex items-center justify-between gap-3 border-b border-line bg-surf-2/50 px-4 py-3", className)}>
      <div className="flex items-center gap-2.5">
        <span className="h-4 w-[3px] rounded-full bg-azure" />
        {icon && <span className="text-azure [&>svg]:h-4 [&>svg]:w-4">{icon}</span>}
        <h2 className="panel-title">{title}</h2>
      </div>
      {right}
    </header>
  );
}

export function Empty({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("px-4 py-8 text-center text-[13px] text-tx-dim", className)}>{children}</div>;
}

export function Stat({ label, value, sub, tone, accent }: {
  label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: string; accent?: string;
}) {
  return (
    <div className="glass relative min-w-0 px-4 py-3.5">
      <span className="absolute inset-x-4 top-0 h-[2px] rounded-b" style={{ background: accent ?? "var(--color-azure)" }} />
      <div className="panel-title truncate !text-[10.5px]">{label}</div>
      <div className={cn("num mt-1.5 truncate text-[22px] font-semibold text-tx-hi", tone)}>{value}</div>
      {sub && <div className="mt-0.5 truncate text-[11px] text-tx-low">{sub}</div>}
    </div>
  );
}

export function Badge({ className, ...p }: React.HTMLAttributes<HTMLSpanElement>) {
  return <span className={cn("inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 font-num text-[10.5px] font-semibold uppercase tracking-wider", className)} {...p} />;
}
