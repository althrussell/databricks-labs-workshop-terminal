import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build output is COMMITTED to ../static — Control Tower deploys the repo
// as-cloned with no build step. Run `make build-frontend` and commit.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
