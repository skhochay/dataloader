import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev:   npm run dev  -> http://localhost:5173, /api proxied to Django on :8080
// Build: npm run build -> Django serves the bundle from loader/static/app/
export default defineConfig(({ command }) => ({
  plugins: [react()],
  base: command === "build" ? "/static/app/" : "/",
  server: { proxy: { "/api": "http://localhost:8080", "/admin": "http://localhost:8080" } },
  build: {
    outDir: "../loader/static/app",
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      output: { entryFileNames: "app.js", assetFileNames: "app[extname]" },
    },
  },
}));
