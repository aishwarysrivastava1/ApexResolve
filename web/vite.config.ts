import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development (npm run dev), /api and /idp go to Caddy on the running containers (make up).
// The browser tests set APEX_API_URL and APEX_IDP_URL to talk to the api and the dev IdP directly.
const apiUrl = process.env.APEX_API_URL ?? "http://localhost:8080";
const idpUrl = process.env.APEX_IDP_URL;

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": apiUrl,
      // Caddy strips the /idp prefix; do the same when talking to the dev IdP directly
      "/idp": idpUrl ? { target: idpUrl, rewrite: (path) => path.replace(/^\/idp/, "") } : apiUrl,
    },
  },
});
