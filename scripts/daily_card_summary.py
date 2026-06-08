#!/usr/bin/env python3
"""Summarize WikiMasters cards opened by recent GitHub Actions runs."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


DEFAULT_WORKFLOW = "open-wikimasters-packs.yml"
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
USER_AGENT = "project-wikimaster-daily-card-summary"

OPENED_CARD_PATTERN = re.compile(r"Opened card:\s*(?P<name>.*?)\s*;\s*rarity=(?P<rarity>.+?)\s*$")
ISO_TIME_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
RARITY_PATTERN = re.compile(r"\b(unknown|UR|SR|PC|L|R|C)\b", re.IGNORECASE)
RARITY_ORDER = {
    "L": 0,
    "UR": 1,
    "SR": 2,
    "R": 3,
    "PC": 4,
    "C": 5,
    "unknown": 99,
}
RARITY_EMOJIS = {
    "L": "\U0001f451",
    "UR": "\U0001f3c6",
    "SR": "\U0001f497",
    "R": "\U0001f49c",
    "PC": "\U0001f535",
    "C": "\u26aa",
    "unknown": "\u2754",
}


@dataclass(frozen=True)
class OpenedCard:
    """One card parsed from an open-packs workflow log."""

    opened_at: datetime
    name: str
    rarity: str
    run_id: int | None = None
    run_url: str = ""


@dataclass
class CardSummary:
    """Aggregated daily summary row for a card name and rarity."""

    name: str
    rarity: str
    count: int = 0
    latest_opened_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=timezone.utc))
    run_urls: dict[int, str] = field(default_factory=dict)


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def parse_github_time(value: str) -> datetime:
    """Parse GitHub/API ISO timestamps as timezone-aware UTC datetimes."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.fromisoformat(truncate_fractional_seconds(text))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def truncate_fractional_seconds(value: str) -> str:
    """Trim ISO fractional seconds to Python's supported microsecond precision."""

    match = re.match(r"(?P<prefix>.*T\d{2}:\d{2}:\d{2})\.(?P<fraction>\d+)(?P<suffix>[+-].*)$", value)
    if not match:
        return value
    return f"{match.group('prefix')}.{match.group('fraction')[:6]}{match.group('suffix')}"


def format_github_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def format_summary_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z")


def extract_line_timestamp(line: str) -> datetime | None:
    match = ISO_TIME_PATTERN.search(line)
    if not match:
        return None
    try:
        return parse_github_time(match.group(0))
    except ValueError:
        return None


def normalize_rarity(value: str) -> str:
    match = RARITY_PATTERN.search(value)
    if not match:
        return "unknown"
    rarity = match.group(1).upper()
    return "unknown" if rarity == "UNKNOWN" else rarity


def display_rarity(rarity: str) -> str:
    normalized = normalize_rarity(rarity)
    return f"{RARITY_EMOJIS.get(normalized, RARITY_EMOJIS['unknown'])} {normalized}"


def parse_opened_card_line(line: str, fallback_opened_at: datetime | None = None) -> OpenedCard | None:
    """Parse one log line emitted by scripts/open_packs.py."""

    match = OPENED_CARD_PATTERN.search(line)
    if not match:
        return None

    opened_at = extract_line_timestamp(line) or fallback_opened_at
    if opened_at is None:
        return None

    name = match.group("name").strip() or "unknown"
    rarity = normalize_rarity(match.group("rarity"))
    return OpenedCard(opened_at=opened_at, name=name, rarity=rarity)


def parse_cards_from_lines(
    lines: Iterable[str],
    *,
    fallback_opened_at: datetime,
    run_id: int | None = None,
    run_url: str = "",
) -> list[OpenedCard]:
    cards: list[OpenedCard] = []
    for line in lines:
        parsed = parse_opened_card_line(line, fallback_opened_at=fallback_opened_at)
        if parsed is None:
            continue
        cards.append(
            OpenedCard(
                opened_at=parsed.opened_at,
                name=parsed.name,
                rarity=parsed.rarity,
                run_id=run_id,
                run_url=run_url,
            )
        )
    return cards


def cards_in_window(cards: Iterable[OpenedCard], cutoff: datetime, now: datetime) -> list[OpenedCard]:
    cutoff_utc = cutoff.astimezone(timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    return [
        card
        for card in cards
        if cutoff_utc <= card.opened_at.astimezone(timezone.utc) <= now_utc
    ]


def aggregate_cards(cards: Iterable[OpenedCard]) -> list[CardSummary]:
    summaries: dict[tuple[str, str], CardSummary] = {}
    for card in cards:
        key = (card.name, card.rarity)
        summary = summaries.setdefault(key, CardSummary(name=card.name, rarity=card.rarity))
        summary.count += 1
        if card.opened_at > summary.latest_opened_at:
            summary.latest_opened_at = card.opened_at
        if card.run_id is not None and card.run_url:
            summary.run_urls[card.run_id] = card.run_url

    return sorted(
        summaries.values(),
        key=lambda summary: (
            RARITY_ORDER.get(summary.rarity, RARITY_ORDER["unknown"]),
            -summary.latest_opened_at.timestamp(),
            summary.name.casefold(),
        ),
    )


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def run_links(summary: CardSummary) -> str:
    links: list[str] = []
    for run_id, url in sorted(summary.run_urls.items(), reverse=True)[:3]:
        links.append(f"[{run_id}]({url})")
    remaining = len(summary.run_urls) - len(links)
    if remaining > 0:
        links.append(f"+{remaining} more")
    return ", ".join(links) if links else ""


def render_summary_markdown(
    summaries: Sequence[CardSummary],
    *,
    cutoff: datetime,
    now: datetime,
    source_run_count: int,
) -> str:
    lines = [
        "## WikiMasters Cards Opened in the Last 24 Hours",
        "",
        f"- Window start: `{format_summary_time(cutoff)}`",
        f"- Window end: `{format_summary_time(now)}`",
        f"- Open-packs workflow runs checked: `{source_run_count}`",
        "",
    ]

    if not summaries:
        lines.append("No cards were opened in the last 24 hours.")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "| Rank | Name | Rarity | Count | Latest opened (UTC) | Runs |",
            "|---:|---|---|---:|---|---|",
        ]
    )
    for index, summary in enumerate(summaries, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    markdown_cell(summary.name),
                    markdown_cell(display_rarity(summary.rarity)),
                    str(summary.count),
                    f"`{format_summary_time(summary.latest_opened_at)}`",
                    run_links(summary),
                ]
            )
            + " |"
        )

    return "\n".join(lines) + "\n"


def repo_owner_name(repository: str) -> tuple[str, str]:
    try:
        owner, repo = repository.split("/", 1)
    except ValueError as exc:
        raise RuntimeError("GITHUB_REPOSITORY must look like OWNER/REPO.") from exc
    if not owner or not repo:
        raise RuntimeError("GITHUB_REPOSITORY must look like OWNER/REPO.")
    return owner, repo


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": USER_AGENT,
    }


def build_github_url(api_url: str, path: str, params: dict[str, str] | None = None) -> str:
    url = f"{api_url.rstrip('/')}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return url


def github_request_bytes(url: str, token: str) -> bytes:
    request = urllib.request.Request(url, headers=github_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed: HTTP {exc.code} {url}\n{body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API request failed: {url}\n{exc}") from exc


def github_request_json(url: str, token: str) -> dict[str, Any]:
    payload = github_request_bytes(url, token)
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub API returned invalid JSON for {url}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"GitHub API returned unexpected JSON for {url}")
    return parsed


def list_recent_workflow_runs(
    *,
    repository: str,
    workflow: str,
    cutoff: datetime,
    api_url: str,
    token: str,
) -> list[dict[str, Any]]:
    owner, repo = repo_owner_name(repository)
    encoded_owner = urllib.parse.quote(owner, safe="")
    encoded_repo = urllib.parse.quote(repo, safe="")
    encoded_workflow = urllib.parse.quote(workflow, safe="")
    path = f"repos/{encoded_owner}/{encoded_repo}/actions/workflows/{encoded_workflow}/runs"

    runs: list[dict[str, Any]] = []
    page = 1
    while True:
        url = build_github_url(
            api_url,
            path,
            {
                "created": f">={format_github_time(cutoff)}",
                "exclude_pull_requests": "true",
                "per_page": "100",
                "page": str(page),
                "status": "completed",
            },
        )
        payload = github_request_json(url, token)
        page_runs = payload.get("workflow_runs", [])
        if not isinstance(page_runs, list):
            raise RuntimeError("GitHub API response did not include a workflow_runs list.")
        runs.extend(run for run in page_runs if isinstance(run, dict))
        if len(page_runs) < 100:
            break
        page += 1

    return runs


def workflow_run_logs_url(*, repository: str, run_id: int, api_url: str) -> str:
    owner, repo = repo_owner_name(repository)
    encoded_owner = urllib.parse.quote(owner, safe="")
    encoded_repo = urllib.parse.quote(repo, safe="")
    return build_github_url(api_url, f"repos/{encoded_owner}/{encoded_repo}/actions/runs/{run_id}/logs")


def iter_log_archive_lines(archive: bytes) -> Iterator[str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as log_zip:
        for name in sorted(log_zip.namelist()):
            if name.endswith("/"):
                continue
            with log_zip.open(name) as raw_file:
                for raw_line in io.TextIOWrapper(raw_file, encoding="utf-8", errors="replace"):
                    yield raw_line.rstrip("\n")


def cards_from_workflow_run(
    *,
    repository: str,
    run: dict[str, Any],
    api_url: str,
    token: str,
) -> list[OpenedCard]:
    run_id = int(run["id"])
    fallback_time = parse_github_time(str(run.get("run_started_at") or run.get("created_at") or run.get("updated_at")))
    run_url = str(run.get("html_url") or "")
    logs_url = workflow_run_logs_url(repository=repository, run_id=run_id, api_url=api_url)
    archive = github_request_bytes(logs_url, token)
    return parse_cards_from_lines(
        iter_log_archive_lines(archive),
        fallback_opened_at=fallback_time,
        run_id=run_id,
        run_url=run_url,
    )


def write_summary(summary_path: str | None, markdown: str) -> None:
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write(markdown)
    else:
        print(markdown, end="")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize WikiMasters cards opened during a rolling window.")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="Workflow file whose logs contain opened cards.")
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS, help="Rolling summary window.")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""), help="GitHub repository as OWNER/REPO.")
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", DEFAULT_API_URL), help="GitHub API base URL.")
    parser.add_argument("--summary-path", default=os.environ.get("GITHUB_STEP_SUMMARY"), help="Markdown output path.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.lookback_hours <= 0:
        raise RuntimeError("--lookback-hours must be positive.")

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing GITHUB_TOKEN; the workflow must pass github.token to this script.")
    if not args.repository:
        raise RuntimeError("Missing GITHUB_REPOSITORY; set --repository or run inside GitHub Actions.")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=args.lookback_hours)
    log(f"Collecting opened cards from {args.workflow} since {format_github_time(cutoff)}.")

    runs = list_recent_workflow_runs(
        repository=args.repository,
        workflow=args.workflow,
        cutoff=cutoff,
        api_url=args.api_url,
        token=token,
    )
    log(f"Found {len(runs)} recent open-packs workflow run(s).")

    cards: list[OpenedCard] = []
    for run in runs:
        cards.extend(cards_from_workflow_run(repository=args.repository, run=run, api_url=args.api_url, token=token))

    recent_cards = cards_in_window(cards, cutoff, now)
    summaries = aggregate_cards(recent_cards)
    markdown = render_summary_markdown(summaries, cutoff=cutoff, now=now, source_run_count=len(runs))
    write_summary(args.summary_path, markdown)
    log(f"Summary complete: {len(recent_cards)} card opening(s), {len(summaries)} unique ranked row(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
