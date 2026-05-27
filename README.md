# WikiMasters Pack Opener

Hourly GitHub Actions automation for opening available WikiMasters packs with Python and Playwright.

## GitHub Setup

Create a GitHub repository and add these repository secrets:

- `WIKIMASTERS_EMAIL`
- `WIKIMASTERS_PASSWORD`

Do not commit credentials to this repository. Rotate the WikiMasters password if it is exposed outside secret storage.

The workflow runs hourly at minute 17 UTC and can also be started manually from the Actions tab.

## Local Smoke Test

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
WIKIMASTERS_EMAIL="your-email" WIKIMASTERS_PASSWORD="your-password" python scripts/open_packs.py
```

Set `HEADLESS=0` for a visible local browser:

```bash
HEADLESS=0 WIKIMASTERS_EMAIL="your-email" WIKIMASTERS_PASSWORD="your-password" python scripts/open_packs.py
```

## Optional Limits

- `MAX_PACKS_PER_RUN` defaults to `10`.
- `MAX_CARD_ADVANCES` defaults to `20`.
