/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During development the Vite dev server proxies API calls to a locally running coordinator
// (HOMEMESH_API, default http://127.0.0.1:8765). In production the coordinator serves the
// built files itself, so the UI and API share one origin and no CORS is needed.
const api = process.env.HOMEMESH_API ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: api, changeOrigin: false, secure: true } },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 900,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
