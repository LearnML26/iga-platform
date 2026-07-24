import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy: the platform's services are ClusterIP-only inside the AKS VNet
// (no ingress until Phase 4.5's APIM), so local development reaches them via
// `scripts/dev-portal.sh`, which port-forwards each service to the ports
// below. The /api/<service> path shape is deliberately the same one an APIM
// front door can expose later, so the client code never changes.
const forward = (port: number) => ({
  target: `http://localhost:${port}`,
  changeOrigin: true,
  rewrite: (path: string) => path.replace(/^\/api\/[^/]+/, ""),
});

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/identity": forward(8081),
      "/api/provisioning": forward(8082),
      "/api/source": forward(8083),
      "/api/rbac": forward(8084),
      "/api/requests": forward(8085),
    },
  },
});
