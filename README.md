# WikiMasters Pack Opener

Python + Playwright automation that opens available WikiMasters card packs on a schedule.

The project is intended to run for free from a private GitHub repository using GitHub Actions. It logs into WikiMasters with repository secrets, visits `https://www.wiki-masters.com/pulls`, opens every available pack, waits 5 seconds after clicking `Ouvrir`, then clicks through the right-arrow card navigation until each pack is finished.

## Project Structure

```text
.
├── .github/workflows/open-wikimasters-packs.yml
├── scripts/open_packs.py
├── requirements.txt
├── .gitignore
└── README.md
```

- `.github/workflows/open-wikimasters-packs.yml` defines the hourly GitHub Actions job and the manual run trigger.
- `scripts/open_packs.py` contains the browser automation.
- `requirements.txt` pins the Python Playwright dependency.
- `.gitignore` keeps secrets, local environments, logs, and generated artifacts out of git.

## Architecture

The automation has two layers:

1. **GitHub Actions scheduler**
   - Runs every hour at minute `17` UTC.
   - Can also be started manually from the GitHub Actions tab.
   - Uses the official Playwright Python container: `mcr.microsoft.com/playwright/python:v1.59.0-noble`.
   - Reads `WIKIMASTERS_EMAIL` and `WIKIMASTERS_PASSWORD` from GitHub repository secrets.
   - Uploads screenshots and metadata only when a run fails.

2. **Playwright browser script**
   - Opens Chromium in headless mode.
   - Navigates to the WikiMasters packs page.
   - Logs in if WikiMasters redirects to the login page.
   - Detects the `Ouvrir` button.
   - Opens all available packs, up to `MAX_PACKS_PER_RUN`.
   - For each pack, waits 5 seconds, then advances through cards using the right arrow.
   - Stops safely if no pack is available, the arrow disappears, the card counter stops changing, or the max click limit is reached.

## GitHub Setup

Create or use a private GitHub repository for this project.

Add the required secrets:

```bash
gh secret set WIKIMASTERS_EMAIL
gh secret set WIKIMASTERS_PASSWORD
```

Paste each value only when the GitHub CLI prompts for it. Do not put the password directly in the command line, because that can leave it in shell history.

Check that the secrets exist:

```bash
gh secret list
```

Expected names:

```text
WIKIMASTERS_EMAIL
WIKIMASTERS_PASSWORD
```

## How To Use

### Run Automatically

The workflow runs automatically every hour:

```yaml
schedule:
  - cron: "17 * * * *"
```

GitHub cron schedules use UTC. The run may start a few minutes late depending on GitHub Actions load.

### Run Manually

In GitHub:

1. Open the repository.
2. Go to **Actions**.
3. Select **Open WikiMasters Packs**.
4. Click **Run workflow**.

The run is successful when the job ends green. It is also considered successful if no packs are available.

### Run Locally

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Run the script with local environment variables:

```bash
WIKIMASTERS_EMAIL="your-email" WIKIMASTERS_PASSWORD="your-password" python scripts/open_packs.py
```

To see the browser while testing locally:

```bash
HEADLESS=0 WIKIMASTERS_EMAIL="your-email" WIKIMASTERS_PASSWORD="your-password" python scripts/open_packs.py
```

## Configuration

Optional environment variables:

- `HEADLESS`: defaults to `1`. Set to `0` locally to show the browser.
- `MAX_PACKS_PER_RUN`: defaults to `10`. Maximum number of packs opened in one run.
- `MAX_CARD_ADVANCES`: defaults to `20`. Maximum right-arrow clicks attempted per pack.
- `ARTIFACT_DIR`: defaults to `artifacts`. Directory for failure screenshots and metadata.

## Failure Artifacts

If the GitHub Actions run fails, it uploads an artifact named:

```text
wikimasters-open-packs-failure
```

The artifact may include:

- a screenshot of the failed page;
- a small text file with the failure reason, timestamp, and current URL.

Full Playwright traces are intentionally not enabled by default because they can capture sensitive page/session state.

## Troubleshooting

- **Secrets missing**: run `gh secret list` and confirm both required secrets exist.
- **Login fails**: verify the credentials, check for CAPTCHA, 2FA, or email verification, and rotate/update the stored password if needed.
- **Workflow does not appear**: make sure `.github/workflows/open-wikimasters-packs.yml` is committed and pushed to GitHub.
- **Hourly job does not run exactly on time**: this is normal for GitHub Actions scheduled workflows.
- **UI changed on WikiMasters**: inspect the failure screenshot artifact and update the Playwright selectors in `scripts/open_packs.py`.

## Security Notes

- Keep the repository private.
- Never commit `.env` files or real credentials.
- Use GitHub repository secrets for credentials.
- Rotate the WikiMasters password if it has been pasted into chat, logs, terminal history, or any non-secret storage.
