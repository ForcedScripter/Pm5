import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "@/components/layout/Shell";
import Login from "@/pages/Login";
import Terminal from "@/pages/Terminal";
import { useAuth } from "@/stores/auth";
import "./index.css";

const Settings = lazy(() => import("@/pages/Settings"));
const Logs = lazy(() => import("@/pages/Logs"));
const Signals = lazy(() => import("@/pages/Signals"));
const History = lazy(() => import("@/pages/History"));

const qc = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } });

function Protected({ children }: { children: React.ReactNode }) {
  const token = useAuth((s) => s.token);
  if (!token) return <Navigate to="/login" replace />;
  return <Shell><Suspense fallback={<div className="p-10 text-center text-tx-dim">Loading…</div>}>{children}</Suspense></Shell>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/" element={<Protected><Terminal /></Protected>} />
          <Route path="/settings" element={<Protected><Settings /></Protected>} />
          <Route path="/history" element={<Protected><History /></Protected>} />
          <Route path="/logs" element={<Protected><Logs /></Protected>} />
          <Route path="/signals" element={<Protected><Signals /></Protected>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
