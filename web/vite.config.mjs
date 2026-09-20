import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiProxy = process.env.HCUOPT_DEV_API_PROXY;

export default defineConfig({
  build: {
    outDir: "dist/client",
  },
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    warmup: {
      clientFiles: ["./src/main.jsx"],
    },
    ...(apiProxy ? { proxy: { "/v1": { target: apiProxy, changeOrigin: false } } } : {}),
  },
  plugins: [react()],
});
