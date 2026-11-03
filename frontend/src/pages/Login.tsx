import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Navigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/form";
import { Api } from "@/services/api";
import { useAuth } from "@/stores/auth";

export default function Login() {
  const { token, setSession } = useAuth();
  const [username, setU] = useState("admin");
  const [password, setP] = useState("");
  const m = useMutation({ mutationFn: () => Api.login(username, password), onSuccess: (r) => setSession(r.token, r.username) });
  if (token) return <Navigate to="/" replace />;
  return (
    <div className="stars flex min-h-screen items-center justify-center p-4">
      <form className="glass glass-gold w-full max-w-sm space-y-5 p-7" onSubmit={(e) => { e.preventDefault(); m.mutate(); }}>
        <div className="flex items-center gap-3">
          <img src="/logo.svg" alt="" className="h-12 w-12 rounded-xl" />
          <div>
            <div className="font-brand text-2xl font-semibold text-amber">pm5bot</div>
            <div className="font-num text-[10px] font-bold tracking-[0.2em] text-azure">SELF-HOSTED CONSOLE</div>
          </div>
        </div>
        <label className="block space-y-1.5"><span className="panel-title">Username</span>
          <Input value={username} onChange={(e) => setU(e.target.value)} autoComplete="username" /></label>
        <label className="block space-y-1.5"><span className="panel-title">Password</span>
          <Input type="password" value={password} onChange={(e) => setP(e.target.value)} autoComplete="current-password" autoFocus /></label>
        {m.error && <p className="text-sm text-down" role="alert">{m.error.message}</p>}
        <Button variant="gold" size="lg" className="w-full" loading={m.isPending} disabled={!password}>Sign in</Button>
        <p className="text-center text-[11px] text-tx-dim">The admin password is <code>ADMIN_PASSWORD</code> from your server's <code>.env</code>.</p>
      </form>
    </div>
  );
}
