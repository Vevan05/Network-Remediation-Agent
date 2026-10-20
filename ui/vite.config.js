import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy /api (including the SSE stream) to the FastAPI server.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://localhost:8000" } },
});
