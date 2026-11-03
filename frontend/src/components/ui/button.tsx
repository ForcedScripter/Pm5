import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-full font-num font-semibold tracking-wide transition-all disabled:pointer-events-none disabled:opacity-45 cursor-pointer",
  {
    variants: {
      variant: {
        gold: "gold-pill hover:brightness-105 active:translate-y-px",
        wallet: "cta-wallet shadow-[0_4px_18px_rgb(255_110_140/0.35)] hover:brightness-105",
        outline: "border border-line-hi bg-surf-1/60 text-tx hover:bg-surf-2 hover:text-tx-hi",
        ghost: "text-tx-mid hover:text-tx-hi hover:bg-surf-2/60",
        amber: "border border-amber/60 bg-amber/10 text-amber hover:bg-amber/20",
        danger: "border border-down/60 bg-down/15 text-down hover:bg-down/25",
      },
      size: { sm: "h-8 px-3 text-xs", md: "h-10 px-5 text-[13px]", lg: "h-12 px-7 text-sm", icon: "h-9 w-9" },
    },
    defaultVariants: { variant: "outline", size: "md" },
  },
);

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild, loading, disabled, children, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp ref={ref} className={cn(buttonVariants({ variant, size }), className)} disabled={disabled || loading} {...props}>
        {loading && <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />}
        {children}
      </Comp>
    );
  },
);
Button.displayName = "Button";
