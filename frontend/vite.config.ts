import react from "@vitejs/plugin-react"
import path from "node:path"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
  server: {
    proxy: {
      "/ws": {
        target: "ws://127.0.0.1:8765",
        ws: true,
      },
      "/history": "http://127.0.0.1:8765",
      "/captions": "http://127.0.0.1:8765",
      "/audio": "http://127.0.0.1:8765",
    },
  },
})
