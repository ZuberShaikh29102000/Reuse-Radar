/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development, /api is proxied to the local Django server so no CORS setup is needed.
// In production the app calls VITE_API_URL (the Render service) directly.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": process.env.REUSE_RADAR_DEV_API ?? "http://127.0.0.1:8000",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
