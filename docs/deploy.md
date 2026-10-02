# Deploying Reuse Radar (free tiers)

The API is read-only. It reads rows that the offline pipeline wrote, so deploying it means
three steps:

1. create a Postgres database
2. load it from the pipeline outputs
3. start the web service

Every service below has a free tier. **Never commit connection strings or keys:** they go
into `.env` locally and the Render dashboard in production.

## 1. Database (Supabase, free)

1. Create a project at supabase.com. Choose a region close to the Render region.
2. In **SQL editor** run `create extension if not exists vector;`. The first migration also does
   this, but it needs the extension to be available on the database.
3. **Project Settings → Database → Connection string → URI.** Use the **connection pooler**
   string (port 6543 for transaction mode, or 5432 for session mode) and append `?sslmode=require`.
   The direct host may be IPv6-only on the free plan, which some hosts cannot reach. Check this
   when deploying.
4. Load the data from your machine:

   ```sh
   # in .env (local, gitignored): DATABASE_URL=<the Supabase URI>
   uv run python manage.py migrate
   uv run python -m reuse_radar.pipeline.store
   ```

   The store stage is idempotent: rerun it after every pipeline run. From a laptop far from the
   database region it is slow (several round trips per row; about 40 minutes for the current
   corpus from India to us-east-1). The nightly GitHub Actions run sits close to the database.
5. **Lock down Supabase's Data API.** Supabase exposes every `public` table through a REST API
   reachable with the project's anon key, which is public by design. Reuse Radar never uses that
   API: Django connects directly as the table owner, which bypasses row-level security. So switch
   RLS on and give the API roles nothing. Run this in the SQL editor after `migrate`:

   ```sql
   do $$ declare t text; begin
     for t in select tablename from pg_tables where schemaname = 'public' loop
       execute format('alter table public.%I enable row level security', t);
     end loop;
   end $$;
   revoke all on all tables in schema public from anon, authenticated;
   revoke all on all sequences in schema public from anon, authenticated;
   alter default privileges in schema public revoke all on tables from anon, authenticated;
   alter default privileges in schema public revoke all on sequences from anon, authenticated;
   ```

   The default-privileges lines cover tables that later migrations create, but RLS must be
   switched on for each new table: rerun the block after a migration that adds one. The Security
   Advisor then shows no "RLS Disabled in Public" errors. Its "Extension in Public" (pgvector) and
   "Unused Index" notices are expected and harmless.

Free-tier notes:
- Supabase pauses free projects after a period of inactivity. The nightly pipeline run (Phase 6
  cron) keeps it active.
- The expected data size, about 100 MB, is well inside the free storage.

## 2. API (Render, free)

1. Push the repository to GitHub.
2. render.com → **New → Blueprint** → select the repository. Render reads `render.yaml`.
3. When asked, enter:
   - `DATABASE_URL`: the same Supabase URI as above
   - `CORS_ALLOWED_ORIGINS`: the frontend URL once it exists (Phase 6). Leave it empty until then.
   - `REVIEWER_TOKENS`: `name:token` for each curator allowed to accept or reject. Generate tokens
     with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and send each curator
     their own token privately. Remove an entry to revoke access.
4. Deploy. Render builds with `uv sync --locked --no-dev`, applies migrations at start-up, and
   checks `GET /healthz`.

Free-tier notes:
- Render's free web services sleep when idle, so the first request after a pause takes a while to
  wake the service.
- Logs are JSON lines, one per request, readable in the Render dashboard.

## 3. Curator UI (Cloudflare Pages, free)

1. dash.cloudflare.com → **Workers & Pages → Create → Pages → Connect to Git** → select the
   repository.
2. Build settings: **root directory** `frontend`, **build command** `npm run build`, **output
   directory** `dist`.
3. Environment variable: `VITE_API_URL` = `https://<service>.onrender.com`.
4. After the first deploy, put the Pages URL (e.g. `https://reuse-radar.pages.dev`) into the
   API's `CORS_ALLOWED_ORIGINS` on Render.

## 4. Check it

```
GET https://<service>.onrender.com/healthz           -> {"status": "ok"}
GET https://<service>.onrender.com/api/stats
GET https://<service>.onrender.com/api/gaps?min_severity=2
GET https://<service>.onrender.com/api/docs          -> interactive OpenAPI docs
```

## Running the API locally

```sh
docker compose up -d db                 # Postgres with pgvector
uv run python manage.py migrate
uv run python -m reuse_radar.pipeline.store
DJANGO_DEBUG=1 DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1 uv run python manage.py runserver
# then open http://127.0.0.1:8000/api/docs
```
