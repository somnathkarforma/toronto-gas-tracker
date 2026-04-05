# Toronto Gas Tracker

A free static dashboard for Toronto gas prices. Runs entirely on free tiers.

**Stack:** Python · GitHub Actions · Chart.js · Google Gemini API · GitHub Pages

---

## Architecture

```
GitHub Actions (cron: daily)
  └─ update_data.py
       ├─ Scrapes Toronto regular price (3 sources + fallback)
       ├─ Updates history.json (185-day rolling window)
       ├─ Fetches energy headlines via Google News RSS
       ├─ Enriches headlines with Gemini (impact + summary)
       └─ Writes data.json

GitHub Pages (static hosting)
  └─ index.html
       ├─ Loads data.json on page open
       ├─ Polls data.json every 30 s (timestamp check)
       ├─ Re-renders charts/cards only when generatedAt changes
       └─ "Refresh now" button for on-demand reload
```

---

## Files

| File | Purpose |
|---|---|
| `update_data.py` | Data pipeline — scraping, news, JSON output |
| `index.html` | Static dashboard — charts, news, auto-refresh |
| `data.json` | Generated daily — consumed by the dashboard |
| `history.json` | Rolling price history — updated daily |
| `.github/workflows/main.yml` | Scheduled CI/CD — runs at 00:00 Toronto time |
| `requirements.txt` | Pinned Python dependencies |

---

## Local development

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your Gemini key (optional — falls back gracefully without it)
export GEMINI_API_KEY=your_key_here   # macOS/Linux
set GEMINI_API_KEY=your_key_here      # Windows CMD

# 3. Run the data pipeline
python update_data.py

# 4. Serve the site
python -m http.server 8000
# → open http://127.0.0.1:8000/
```

> **Windows note:** If `python` defaults to the free-threaded 3.13t build, use `py -3.11` instead.

---

## GitHub setup (first-time deployment)

### Step 1 — Create the repository

1. Go to [github.com/new](https://github.com/new)
2. Name it `toronto-gas-tracker` (or any name)
3. Set visibility to **Public** (required for free GitHub Pages)
4. Do **not** initialise with a README — you'll push your own files

### Step 2 — Add the Gemini API secret

1. In your repo, go to **Settings → Secrets and variables → Actions**
2. Click **New repository secret**
3. Name: `GEMINI_API_KEY`  Value: your Gemini API key
4. Click **Add secret**

> The dashboard works without this key — news items will show a generic summary.

### Step 3 — Push code from VS Code

```bash
# In VS Code terminal (Ctrl+` to open)
git init
git remote add origin https://github.com/YOUR_USERNAME/toronto-gas-tracker.git
git add .
git commit -m "Initial commit"
git push -u origin main
```

### Step 4 — Enable GitHub Pages

1. Go to **Settings → Pages**
2. Under **Source**, select **Deploy from a branch**
3. Branch: `gh-pages` · Folder: `/ (root)`
4. Click **Save**

> The `gh-pages` branch is created automatically by the Actions workflow on first run.

### Step 5 — Run the workflow manually

1. Go to **Actions → Update Gas Tracker**
2. Click **Run workflow → Run workflow**
3. Wait ~2 minutes for it to complete
4. Your site is live at: `https://YOUR_USERNAME.github.io/toronto-gas-tracker/`

---

## Updating dependencies

```bash
pip install --upgrade beautifulsoup4 requests google-generativeai lxml
pip freeze | grep -E "beautifulsoup4|requests|google-generativeai|lxml"
# Copy the output lines into requirements.txt
```

---

## Cron schedule

The workflow runs at `0 4 * * *` UTC, which is midnight Toronto time during EDT (UTC-4).
In winter (EST = UTC-5), prices update at 11 PM the previous night.
To adjust for EST: change the cron to `0 5 * * *`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `data.json` not updating | Check **Actions** tab for workflow errors |
| Price shows "Hardcoded fallback" | All three scrape sources blocked — check logs |
| News shows no summaries | `GEMINI_API_KEY` secret missing or invalid |
| Site shows old data | GitHub Pages CDN cache — wait 2-3 min after deploy |
| `gh-pages` branch missing | Run workflow manually once to create it |
