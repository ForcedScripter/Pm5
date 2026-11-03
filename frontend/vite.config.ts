/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.BACKEND_URL ?? "http://127.0.0.1:8000", changeOrigin: true },
             "/ws": { target: process.env.BACKEND_URL ?? "http://127.0.0.1:8000", ws: true } },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          charts: ["lightweight-charts"],
          ui: ["framer-motion", "@radix-ui/react-dialog", "@radix-ui/react-tabs", "@radix-ui/react-tooltip", "@radix-ui/react-switch"],
          data: ["@tanstack/react-query", "zustand"],
        },
      },
    },
  },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test-setup.ts"] },
});
