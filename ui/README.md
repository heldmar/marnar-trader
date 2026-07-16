# MarNar Trader — Web UI (Sprint 6)

React + TypeScript + Vite SPA served by nginx, which also proxies `/api`
same-origin to the core service (core is never exposed publicly).

Design brief: `Context/UI-DESIGN-BRIEF.md` (UI-01..UI-15 — sage/cream, light
only, non-financial audience, onion layering, visual-first).

## Dev

```sh
npm install
npm run dev        # expects the core service on localhost:8000 (proxied)
npm run build      # type-check + production bundle
npm run lint
```

## Container

`Dockerfile` is multi-stage (node build → nginx:alpine, arm64-compatible).
Local full stack: `docker compose -f docker-compose.yml -f docker-compose.local.yml up --build`
→ UI at http://localhost:18081, core at http://localhost:18080.
