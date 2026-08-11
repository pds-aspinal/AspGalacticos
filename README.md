# FPL League Tracker — Setup

League: https://fantasy.premierleague.com/en/leagues/487804/standings (ID: `487804`)

Total time: ~20 minutes, all free tier.

## 1. Create the Supabase project

1. Go to https://supabase.com → **New project** (free tier is plenty for this).
2. Once it's created, go to **SQL Editor** → **New query**.
3. Paste in the contents of `schema.sql` from this folder and run it.
4. Go to **Project Settings → API**. You'll need two values later:
   - **Project URL** (e.g. `https://xxxx.supabase.co`)
   - **anon (public) key** — safe to expose in the dashboard, read-only
   - **service_role key** — keep this secret, it can write. Only goes in GitHub Secrets, never in the HTML.

## 2. Put this project on GitHub

1. Create a new **public** GitHub repo (public is required for free GitHub Pages).
2. Push these files to it:
   ```
   schema.sql
   collector.py
   requirements.txt
   .github/workflows/collect.yml
   dashboard/index.html
   README.md
   ```

## 3. Add your secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add three:

| Name | Value |
|---|---|
| `SUPABASE_URL` | your Project URL |
| `SUPABASE_SERVICE_KEY` | your service_role key |
| `LEAGUE_ID` | `487804` |

## 4. Run the collector once, manually

1. Go to the **Actions** tab in your repo → **Collect FPL standings** → **Run workflow**.
2. Check it goes green. If it fails, click in to see the error — most likely cause is a typo'd secret.
3. In Supabase, go to **Table Editor → gameweek_snapshots** and confirm rows appeared.

From here it runs automatically every Tuesday at 06:00 UTC (safely after Monday Night Football, so the gameweek's fully settled). You can also trigger it manually any time from the Actions tab.

## 5. Wire up and publish the dashboard

1. Open `dashboard/index.html` and replace:
   ```js
   const SUPABASE_URL = "YOUR_SUPABASE_URL";
   const SUPABASE_ANON_KEY = "YOUR_SUPABASE_ANON_KEY";
   ```
   with your real Project URL and **anon key** (not the service key — this file is public).
2. Commit and push.
3. In the repo: **Settings → Pages → Source → Deploy from a branch**, pick your main branch and the `/dashboard` folder (or `/` if you move `index.html` to the repo root — GitHub Pages only serves from root or `/docs`, so if `/dashboard` isn't offered as an option, move `index.html` to the repo root instead).
4. Your league will be live at `https://<your-username>.github.io/<repo-name>/`.

## 6. Once you've decided prize rules

The `weekly_winner` and `weekly_loser` views already cover the two most common rules (highest/lowest score each gameweek) — nothing extra needed for those.

For anything more specific (monthly prizes, longest streak, most improved), tell me the exact rule and I'll add a view or a row in `prize_rules`/`prize_winners` for it — the schema's built so that's additive, not a rebuild.

## Notes / things that could break

- The FPL API is unofficial — it's what every third-party FPL site uses and has been stable for years, but it's not guaranteed by anyone. If the collector suddenly starts failing, check the Actions log first.
- `collector.py` re-fetches each team's *entire* history every run rather than just the latest gameweek. For a private league (a handful of teams) this is a handful of HTTP requests and takes seconds — not worth optimizing.
- The `teams` fetch only reads the first page of standings (~50 entries). Fine for any private league; flag if yours is ever bigger than that.
