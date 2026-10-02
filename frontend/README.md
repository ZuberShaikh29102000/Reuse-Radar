# Reuse Radar — curator UI

React + TypeScript (Vite) front end for the triage queue: gaps ranked by severity, filters,
expandable verbatim evidence, paper profiles, and Accept/Reject buttons that write curator
reviews.

## Develop

```sh
npm install
# API running locally (python manage.py runserver) — /api is proxied to it:
npm run dev                      # http://localhost:5173
REUSE_RADAR_DEV_API=http://127.0.0.1:8765 npm run dev   # if the API runs on another port
```

## Check

```sh
npm run typecheck && npm run lint && npm test && npm run build
```

## Deploy (Cloudflare Pages, free)

- Build command: `npm run build` · Output directory: `dist` · Root directory: `frontend`
- Environment variable: `VITE_API_URL=https://<service>.onrender.com`
- Then add the Pages URL to the API's `CORS_ALLOWED_ORIGINS` on Render.

Curators paste their personal token (from the API's `REVIEWER_TOKENS`) into the "Curator
token" field. It is kept in `sessionStorage` and sent only to the API, as a Bearer token.
