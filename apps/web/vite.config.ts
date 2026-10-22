import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const coreUrl = process.env.NEXA_CORE_URL ?? "http://localhost:8000";
const runtimeUrl = process.env.NEXA_RUNTIME_URL ?? "http://localhost:8001";

// Paths owned by Runtime; everything else under /api belongs to Core.
// This mirrors the production edge routing table (architecture doc 18.2), so the browser
// talks to one origin in development exactly as it will in production.
const runtimePrefixes = [
  "ai",
  "conversations",
  "messages",
  "memories",
  "tools",
  "runs",
  "approvals",
  "search",
  "realtime",
  "runtime",
];

export default defineConfig({
  plugins: [react()],
  test: { environment: "jsdom" },
  define: {
    __NEXA_CORE_URL__: JSON.stringify(coreUrl),
    __NEXA_RUNTIME_URL__: JSON.stringify(runtimeUrl),
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Core owns the usage records, so /ai/usage is the one /ai path served by Django.
      "/api/v1/ai/usage": { target: coreUrl },
      [`^/api/v1/(${runtimePrefixes.join("|")})(/|$)`]: { target: runtimeUrl, ws: true },
      "/api": { target: coreUrl },
      // Development-only aliases for each service's own health endpoints (status page).
      "/__core": { target: coreUrl, rewrite: (path) => path.replace(/^\/__core/, "") },
      "/__runtime": { target: runtimeUrl, rewrite: (path) => path.replace(/^\/__runtime/, "") },
    },
  },
});
