# WikiMasters Pack Opener

Python + Playwright automation that opens available WikiMasters card packs from a GitHub Actions workflow triggered by cron-job.org.

The project is intended to run for free from a private GitHub repository using GitHub Actions as the runner and cron-job.org as the hourly scheduler. It logs into WikiMasters with repository secrets, visits `https://www.wiki-masters.com/pulls`, opens every available pack, waits until the card counter appears, then clicks through the right-arrow card navigation until each pack is finished.

## Project Structure

```text
.
├── .github/workflows/open-wikimasters-packs.yml
├── scripts/open_packs.py
├── requirements.txt
├── .gitignore
└── README.md
```

- `.github/workflows/open-wikimasters-packs.yml` defines the manually dispatchable GitHub Actions job.
- `scripts/open_packs.py` contains the browser automation.
- `requirements.txt` pins the Python Playwright dependency.
- `.gitignore` keeps secrets, local environments, logs, and generated artifacts out of git.

## Architecture

The automation has three layers:

1. **cron-job.org scheduler**
   - Sends an hourly HTTP `POST` request to GitHub's `workflow_dispatch` API.
   - Uses a limited fine-grained GitHub token with access only to this repository.
   - Replaces GitHub's native `schedule` trigger, which can be delayed or skipped.

2. **GitHub Actions runner**
   - Starts when cron-job.org calls `workflow_dispatch`.
   - Can also be started manually from the GitHub Actions tab.
   - Uses the official Playwright Python container: `mcr.microsoft.com/playwright/python:v1.59.0-noble`.
   - Reads `WIKIMASTERS_EMAIL` and `WIKIMASTERS_PASSWORD` from GitHub repository secrets.
   - Uploads screenshots and metadata only when a run fails.

3. **Playwright browser script**
   - Opens Chromium in headless mode.
   - Navigates to the WikiMasters packs page.
   - Logs in if WikiMasters redirects to the login page.
   - Detects the `Ouvrir` button.
   - Opens all available packs, up to `MAX_PACKS_PER_RUN`.
   - For each pack, waits for the card counter, then advances through cards using the right arrow.
   - Logs each opened card as `Opened card: <name> ; rarity=<rarity>`.
   - Writes a GitHub Actions step summary table with card name and rarity.
   - Stops safely if no pack is available, the arrow disappears, the card counter stops changing, or the max click limit is reached.

## GitHub Setup

Create or use a private GitHub repository for this project.

Add the WikiMasters login secrets:

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

Create a fine-grained GitHub token for cron-job.org:

- Repository access: only `JeanMichelChien/project_wikimaster`.
- Repository permissions: `Actions: Read and write`.
- Expiration: 90 days, then rotate the token.

## How To Use

### Run Automatically With cron-job.org

Create one cron-job.org job that calls GitHub's workflow dispatch endpoint.

Recommended schedule:

```text
Every hour at minute 43
```

Request URL:

```text
https://api.github.com/repos/JeanMichelChien/project_wikimaster/actions/workflows/open-wikimasters-packs.yml/dispatches
```

Request method:

```text
POST
```

Headers:

```text
Authorization: Bearer <YOUR_FINE_GRAINED_GITHUB_TOKEN>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2026-03-10
Content-Type: application/json
User-Agent: wikimasters-cron-job
```

Request body:

```json
{"ref":"main"}
```

Any `2xx` GitHub API response should be treated as success. After cron-job.org runs, the GitHub Actions run should appear as `workflow_dispatch`.

### Run Manually

In GitHub:

1. Open the repository.
2. Go to **Actions**.
3. Select **Open WikiMasters Packs**.
4. Click **Run workflow**.

The run is successful when the job ends green. It is also considered successful if no packs are available.

You can also trigger it with the GitHub CLI:

```bash
gh workflow run open-wikimasters-packs.yml --ref main
```

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
- `PACK_OPEN_MAX_WAIT_MS`: defaults to `5000`. Maximum wait for the card counter after clicking `Ouvrir`.
- `CARD_ADVANCE_DELAY_MS`: defaults to `350`. Delay after each right-arrow click before reading the next card counter.
- `RIGHT_ARROW_ROLE_TIMEOUT_MS`: defaults to `100`. Short accessibility-selector fallback timeout for the right-arrow button.
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
- **cron-job.org run succeeds but no GitHub Action appears**: check the fine-grained token permissions, request URL, request body, and `Authorization` header.
- **GitHub API returns 401 or 403**: rotate the fine-grained token and verify it has `Actions: Read and write` on only this repository.
- **GitHub API returns 404**: verify the repository owner/name and workflow file name in the URL.
- **UI changed on WikiMasters**: inspect the failure screenshot artifact and update the Playwright selectors in `scripts/open_packs.py`.

## Security Notes

- Keep the repository private.
- Never commit `.env` files or real credentials.
- Use GitHub repository secrets for credentials.
- Store the cron-job.org GitHub token only in cron-job.org.
- Use a fine-grained token with access only to this repository and only `Actions: Read and write`.
- Rotate the cron-job.org GitHub token every 90 days.
- Rotate the WikiMasters password if it has been pasted into chat, logs, terminal history, or any non-secret storage.
