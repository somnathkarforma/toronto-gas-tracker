# Toronto Gas Tracker

A free static dashboard for Toronto gas prices using:

- **Python** for scraping and data generation
- **GitHub Actions** for daily automation
- **Chart.js** for interactive charts
- **Google Gemini API** for short news summaries
- **GitHub Pages** for free hosting

## Files

- `update_data.py` - fetches gas headlines, updates history, writes `data.json`, and falls back gracefully if a source blocks scraping
- `index.html` - the public dashboard
- `.github/workflows/main.yml` - daily automation and Pages deployment
- `requirements.txt` - Python dependencies

## Local run

```bash
py -3.13 -m pip install -r requirements.txt
py -3.13 update_data.py
py -3.13 -m http.server 8000
```

Then open `http://127.0.0.1:8000/` in a browser.

> On Windows, use the standard `py -3.13` interpreter if `py -3` defaults to the free-threaded `3.13t` build.

## GitHub setup

1. Create a repository secret named `GEMINI_API_KEY`.
2. Push the repo to GitHub.
3. In **Settings > Pages**, enable GitHub Pages for the deployed branch.
4. Run the workflow once with **Actions > Update Gas Tracker > Run workflow**.

Your site will be available at:

```text
https://<your-username>.github.io/toronto-gas-tracker/
```
