# WikiMasters Pack Opener

Python + Playwright automation that opens available WikiMasters card packs from a GitHub Actions workflow triggered by cron-job.org.

The project can run from a GitHub repository using GitHub Actions as the runner and cron-job.org as the hourly scheduler. It logs into WikiMasters with repository secrets, visits `https://www.wiki-masters.com/pulls`, opens every available pack, waits until the card counter appears, then clicks through the right-arrow card navigation until each pack is finished.

## Project Structure

```text
.
├── .github/workflows/open-wikimasters-packs.yml
├── scripts/open_packs.py
├── scripts/tag_collection_cards.py
├── scripts/sell_shitty_cards.py
├── tests/
├── requirements.txt
├── .gitignore
└── README.md
```

- `.github/workflows/open-wikimasters-packs.yml` defines the manually dispatchable GitHub Actions job.
- `scripts/open_packs.py` contains the browser automation.
- `scripts/tag_collection_cards.py` scans the collection and tags supported topic cards.
- `scripts/sell_shitty_cards.py` auctions cards tagged `à bicrave`.
- `tests/` contains unit tests for non-browser logic.
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
   - Logs each opened card as `Opened card: <name> ; rarity=<emoji> <rarity>`.
   - Writes a GitHub Actions step summary table sorted from rarest to most common.
   - Rarity emojis are ordered from best to worst: `👑 L`, `🏆 UR`, `💗 SR`, `💜 R`, `🔵 PC`, `⚪ C`.
   - Stops safely if no pack is available, the arrow disappears, the card counter stops changing, or the max click limit is reached.

## GitHub Setup

Create or use a GitHub repository for this project.

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

- Repository access: only the target repository.
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
https://api.github.com/repos/OWNER/REPO/actions/workflows/open-wikimasters-packs.yml/dispatches
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
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

Run the script with local environment variables:

```bash
python3 scripts/open_packs.py
```

To see the browser while testing locally:

```bash
HEADLESS=0 python3 scripts/open_packs.py
```

For local runs, both scripts load credentials from `.env` automatically. GitHub does not expose secret values after they are saved, so fill `.env` once with the same values you originally stored as repository secrets:

```dotenv
WIKIMASTERS_EMAIL=you@example.com
WIKIMASTERS_PASSWORD=your-password
HEADLESS=1
```

### Tag Collection Cards

The tagger is script-only and does not have a GitHub Actions workflow. It logs in with the same `WIKIMASTERS_EMAIL` and `WIKIMASTERS_PASSWORD` environment variables, scans `https://www.wiki-masters.com/collection` once, uses cached Wikipedia metadata, and writes a grouped report to `artifacts/tag_report.md`. It also writes reusable candidates to `artifacts/tag_candidates.json`, so applying after review can skip the full scan.

Dry run is the default and does not change WikiMasters. By default it classifies all supported tags: `plante`, `philo`, `scam`, `train`, `souterrains`, and `à bicrave`.

```bash
python3 scripts/tag_collection_cards.py
```

Limit the dry-run report to selected tags:

```bash
python3 scripts/tag_collection_cards.py --tags philo,scam --sample-per-tag 10
```

Recommended local workflow:

```bash
python3 scripts/tag_collection_cards.py --tags plante
less artifacts/tag_report.md
python3 scripts/tag_collection_cards.py --apply-candidates --batch-size 1
```

The final command applies from `artifacts/tag_candidates.json` and only visits candidate pages. To force a fresh scan and apply in one command, use:

```bash
python3 scripts/tag_collection_cards.py --apply --tags plante,philo --batch-size 5
```

The `à bicrave` tag is intentionally for shitty cards that should be sold. It only matches cards with no other existing tag, rarity `C` or `PC`, and one of these topics: villages/towns, political people, TV shows, movies, sport athletes, or pornographic cards.

```bash
python3 scripts/tag_collection_cards.py --tags "à bicrave"
less artifacts/tag_report.md
python3 scripts/tag_collection_cards.py --apply-candidates --tags "à bicrave" --batch-size 5
```

For a small local test:

```bash
HEADLESS=0 python3 scripts/tag_collection_cards.py --max-cards 50
```

### Sell Shitty Cards

The seller script uses normal WikiMasters UI interactions. It filters the collection to cards tagged `à bicrave`, opens up to 5 cards per cycle, clicks **Mettre aux enchères**, sets **Mise de départ** to `10` for `C`/`PC` cards and `40` for any manually tagged higher-rarity card, selects **Durée** `10 min`, and clicks **Lancer l'enchère**. After an apply cycle it waits `10` minutes and `10` seconds before trying the next 5 cards.

Dry run is the default:

```bash
python3 scripts/sell_shitty_cards.py
```

Launch one real batch of up to 5 auctions:

```bash
python3 scripts/sell_shitty_cards.py --apply --cycles 1
```

Launch batches repeatedly until no new `à bicrave` cards remain:

```bash
python3 scripts/sell_shitty_cards.py --apply
```

## Configuration

Optional environment variables:

- `HEADLESS`: defaults to `1`. Set to `0` locally to show the browser.
- `MAX_PACKS_PER_RUN`: defaults to `10`. Maximum number of packs opened in one run.
- `MAX_CARD_ADVANCES`: defaults to `20`. Maximum right-arrow clicks attempted per pack.
- `PACK_OPEN_MAX_WAIT_MS`: defaults to `5000`. Maximum wait for the card counter after clicking `Ouvrir`.
- `CARD_ADVANCE_DELAY_MS`: defaults to `350`. Delay after each right-arrow click before reading the next card counter.
- `CARD_DETAILS_MAX_WAIT_MS`: defaults to `1200`. Maximum extra wait for card name and rarity after the card counter appears.
- `RIGHT_ARROW_ROLE_TIMEOUT_MS`: defaults to `100`. Short accessibility-selector fallback timeout for the right-arrow button.
- `ARTIFACT_DIR`: defaults to `artifacts`. Directory for failure screenshots and metadata.

Collection tagger CLI options:

- `--dry-run`: default. Report candidates without changing tags.
- `--apply`: apply matching tags through the WikiMasters bulk selection UI.
- `--apply-candidates`: apply tags from `artifacts/tag_candidates.json` without rescanning the full collection.
- `--tags`: comma-separated supported tags. Defaults to `plante,philo,scam,train,souterrains,à bicrave`.
- `--sample-per-tag`: defaults to `10`. Limits rows shown per tag in the report only; it does not limit scanning or applying. Use `0` to show all candidates.
- `--max-cards`: defaults to `0`, meaning scan all loaded collection cards.
- `--batch-size`: defaults to `8`.
- `--max-apply-candidates`: defaults to `0`, meaning apply every candidate for each enabled tag. Use `1` for a single-card live smoke test.
- `--scroll-delay-ms`, `--selection-delay-ms`, `--batch-delay-ms`, and `--wikipedia-delay-ms`: throttling controls.
- `--jitter-ms`: defaults to `80`. Adds up to this many random milliseconds after UI actions. Use `0` to disable.
- `--cache-path`: defaults to `artifacts/wikimasters_wikipedia_cache.json`. If that file is missing, the script reuses the legacy `artifacts/plant_wikipedia_cache.json` cache before writing the new cache path.
- `--report-path`: defaults to `artifacts/tag_report.md`.
- `--candidate-path`: defaults to `artifacts/tag_candidates.json`.

Seller CLI options:

- `--dry-run`: default. Open candidates and report what would be auctioned without launching auctions.
- `--apply`: launch auctions through the WikiMasters UI.
- `--tag`: defaults to `à bicrave`.
- `--max-cards-per-cycle`: defaults to `5`, and cannot exceed `5`.
- `--scan-limit`: defaults to `50` tagged cards scanned per cycle.
- `--cycles`: defaults to `0`, meaning repeat until no new tagged cards remain. Use `--cycles 1` for one batch.
- `--wait-seconds`: defaults to `610`, which is 10 minutes and 10 seconds.
- `--start-price`: defaults to `10` for `C`/`PC` cards.
- `--non-low-rarity-start-price`: defaults to `40` for manually tagged non-`C`/`PC` cards.
- `--duration`: defaults to `10 min`.
- `--jitter-ms`: defaults to `80`. Adds up to this many random milliseconds after UI actions. Use `0` to disable.

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

- Keep credentials and local runtime artifacts out of the repository.
- Never commit `.env` files or real credentials.
- Use GitHub repository secrets for credentials.
- Store the cron-job.org GitHub token only in cron-job.org.
- Use a fine-grained token with access only to this repository and only `Actions: Read and write`.
- Rotate the cron-job.org GitHub token every 90 days.
- Rotate the WikiMasters password if it has been pasted into chat, logs, terminal history, or any non-secret storage.
