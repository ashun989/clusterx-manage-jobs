import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: process.env.CLUSTERX_WEB_OUT_DIR || "dist",
    emptyOutDir: true,
  },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
