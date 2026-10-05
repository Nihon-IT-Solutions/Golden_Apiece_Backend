# Golden Apiece MLM — Backend (FastAPI + Supabase Postgres)

```powershell
Copy-Item .env.example .env   # then put your Supabase "Session pooler" connection string in DATABASE_URL
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

* Swagger docs: http://localhost:8000/docs
* Tables, the admin account (`GOLDADMIN` / `Admin@123`, transaction password `12345678`), packages, commission levels
  and optional demo data are created automatically on first start (`app/seed.py`, also runnable with `uv run python -m app.seed`).

See the main `README.md` in the project root for the full setup guide.
