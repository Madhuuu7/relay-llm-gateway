import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The gateway serves the built console from web/dist and proxies nothing in
// production; in development vite proxies the API so the console reloads
// without a rebuild.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  server: {
    port: 5174,
    proxy: {
      "/stats": "http://127.0.0.1:8000",
      "/requests": "http://127.0.0.1:8000",
      "/admin": "http://127.0.0.1:8000",
      "/demo": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
    },
  },
});
