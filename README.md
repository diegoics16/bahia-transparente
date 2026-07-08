# Bahía Transparente

Live dashboard + scheduled data pipeline for Quintero-Puchuncaví Bay water quality monitoring.

## What's in this folder

| File | What it does |
|---|---|
| `index.html` | The public dashboard. Static file, no build step. |
| `schema.sql` | Paste into Supabase's SQL editor once, to create all tables + security rules. |
| `snifa_scraper.py` | Pulls facility inspection/sanction data from SNIFA (SMA). Runs weekly via GitHub Actions. |
| `build_poal_dataset.py` | Downloads + cleans DIRECTEMAR's POAL water quality data. Runs monthly via GitHub Actions. |
| `.github/workflows/` | The two schedules that run the scripts above automatically. |
| `requirements.txt` | Python dependencies for both scripts. |

## One-time setup

Full beginner walkthrough is in the project chat — short version:

1. Create a Supabase project, run `schema.sql` in its SQL editor.
2. Copy the Project URL + `anon` key into `index.html` (top of the `<script>` block).
3. Push this folder to a new GitHub repo.
4. Add `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (the **service_role** key, not anon) as repo secrets:
   Settings → Secrets and variables → Actions → New repository secret.
5. Connect the repo to Cloudflare Pages (or Netlify) to publish `index.html`.
6. Run each workflow once manually (Actions tab → select workflow → "Run workflow") to load real data before you rely on the weekly/monthly schedule.

## Local development / debugging

```
python -m pip install -r requirements.txt
playwright install chromium

# to watch the browser instead of running headless:
set SNIFA_HEADFUL=1        # Windows
export SNIFA_HEADFUL=1     # Mac/Linux

set SUPABASE_URL=https://xxxx.supabase.co
set SUPABASE_SERVICE_KEY=your-service-role-key

python snifa_scraper.py
python build_poal_dataset.py
```

## Data scope, deliberately

- `snifa_scraper.py` currently targets CODELCO, ENAP, and AES ANDES in Puchuncaví/Quintero. ESVAL reports to SISS, not SNIFA — tracked separately, not in this script.
- `build_poal_dataset.py` downloads and cleans the *full* national POAL dataset locally, but only pushes rows for the bay + real comparison sites (Quintero, Puchuncaví, Quintay, Zapallar) to the live database — see `SUPABASE_PUSH_BODIES` in the script.
- Every scraper run writes a row to `sync_runs`, success or failure. Check that table if the dashboard looks stale.
