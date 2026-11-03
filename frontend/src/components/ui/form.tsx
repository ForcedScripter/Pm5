import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { HelpCircle } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/utils";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...p }, ref) => (
    <input ref={ref}
      className={cn("h-10 w-full rounded-xl border border-line-mid bg-bg-0/70 px-3.5 text-[13px] text-tx-hi placeholder:text-tx-dim focus:border-amber/70 focus:outline-none", className)}
      {...p} />
  ),
);
Input.displayName = "Input";

export function Switch({ className, ...p }: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      className={cn("relative h-6 w-11 shrink-0 cursor-pointer rounded-full border border-line-hi bg-surf-3/70 transition-colors data-[state=checked]:border-up/60 data-[state=checked]:bg-up/30 disabled:opacity-40", className)}
      {...p}>
      <SwitchPrimitive.Thumb className="block h-[18px] w-[18px] translate-x-[2px] rounded-full bg-tx-mid shadow transition-transform data-[state=checked]:translate-x-[21px] data-[state=checked]:bg-up" />
    </SwitchPrimitive.Root>
  );
}

export function Hint({ children }: { children: React.ReactNode }) {
  return (
    <TooltipPrimitive.Provider delayDuration={150}>
      <TooltipPrimitive.Root>
        <TooltipPrimitive.Trigger asChild>
          <button type="button" className="text-tx-dim hover:text-tx-mid" aria-label="help"><HelpCircle className="h-3.5 w-3.5" /></button>
        </TooltipPrimitive.Trigger>
        <TooltipPrimitive.Portal>
          <TooltipPrimitive.Content sideOffset={6} className="z-50 max-w-72 rounded-xl border border-line-hi bg-bg-0 px-3 py-2 text-xs leading-relaxed text-tx shadow-xl">
            {children}
          </TooltipPrimitive.Content>
        </TooltipPrimitive.Portal>
      </TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  );
}

/** Labeled numeric field matching the reference "label ⓘ ......... [ value ]" row. */
export function NumberField({ label, hint, value, onChange, step = 0.01, min, max, suffix }: {
  label: string; hint?: string; value: number | null | undefined; onChange: (v: number | null) => void;
  step?: number; min?: number; max?: number; suffix?: string;
}) {
  const id = React.useId();
  return (
    <div className="flex items-center justify-between gap-3">
      <label htmlFor={id} className="flex items-center gap-1.5 text-[13px] text-tx">{label}{hint && <Hint>{hint}</Hint>}</label>
      <div className="flex items-center gap-1.5">
        <input id={id} type="number" step={step} min={min} max={max} value={value ?? ""}
          onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
          className="num h-9 w-24 rounded-full border border-line-mid bg-bg-0/80 px-3 text-center text-[13px] font-semibold text-tx-hi focus:border-amber/70 focus:outline-none" />
        {suffix && <span className="w-6 text-[11px] text-tx-dim">{suffix}</span>}
      </div>
    </div>
  );
}

/** Big segmented choice cards (SELECTIVE / BALANCED / AGGRESSIVE style). */
export function Segmented<T extends string>({ value, options, onChange, cols }: {
  value: T; options: { value: T; label: string; sub?: string }[]; onChange: (v: T) => void; cols?: number;
}) {
  return (
    <div className="grid gap-2.5" style={{ gridTemplateColumns: `repeat(${cols ?? options.length}, minmax(0,1fr))` }} role="radiogroup">
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={value === o.value} onClick={() => onChange(o.value)}
          className={cn("rounded-2xl border px-3 py-3 text-center transition-all",
            value === o.value ? "border-amber-hi/60 bg-gradient-to-b from-amber/20 to-amber/5 text-tx-hi shadow-[0_0_0_1px_rgb(255_211_110/0.25)]"
              : "border-line-mid bg-surf-1/50 text-tx-mid hover:border-line-hi hover:text-tx")}>
          <div className="font-num text-[12.5px] font-bold uppercase tracking-wider">{o.label}</div>
          {o.sub && <div className="mt-0.5 text-[10.5px] text-tx-low">{o.sub}</div>}
        </button>
      ))}
    </div>
  );
}

export function Chips<T extends string | number>({ values, options, onToggle, tone = "amber" }: {
  values: T[]; options: { value: T; label: React.ReactNode }[]; onToggle: (v: T) => void; tone?: "amber" | "down";
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {options.map((o) => {
        const on = values.includes(o.value);
        return (
          <button key={String(o.value)} type="button" aria-pressed={on} onClick={() => onToggle(o.value)}
            className={cn("rounded-full border px-3 py-1.5 font-num text-xs font-semibold transition-colors",
              on ? (tone === "down" ? "border-down/60 bg-down/15 text-down" : "border-amber/60 bg-amber/15 text-amber")
                : "border-line-mid bg-surf-1/50 text-tx-low hover:text-tx")}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function FieldGroup({ title, hint, children, className }: { title: string; hint?: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("space-y-3", className)}>
      <div className="flex items-center gap-1.5 font-num text-[11.5px] font-semibold uppercase tracking-[0.14em] text-tx-mid">{title}{hint && <Hint>{hint}</Hint>}</div>
      {children}
    </div>
  );
}
