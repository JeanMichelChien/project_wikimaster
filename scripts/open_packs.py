#!/usr/bin/env python3
"""Open available WikiMasters packs and reveal every card.

This script logs into WikiMasters, opens packs from the pulls page until none
are available or the configured limit is reached, records each visible card,
and writes a GitHub Actions summary when running in CI.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from scripts.env_loader import load_env_file
except ModuleNotFoundError:
    from env_loader import load_env_file

from playwright.sync_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


load_env_file(Path(os.environ.get("ENV_FILE", Path(__file__).resolve().parents[1] / ".env")))

BASE_URL = "https://www.wiki-masters.com"
PULLS_URL = f"{BASE_URL}/pulls"
ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", "artifacts"))
MAX_PACKS_PER_RUN = int(os.environ.get("MAX_PACKS_PER_RUN", "10"))
MAX_CARD_ADVANCES = int(os.environ.get("MAX_CARD_ADVANCES", "20"))
PACK_OPEN_MAX_WAIT_MS = int(os.environ.get("PACK_OPEN_MAX_WAIT_MS", "5_000"))
CARD_ADVANCE_DELAY_MS = int(os.environ.get("CARD_ADVANCE_DELAY_MS", "350"))
CARD_DETAILS_MAX_WAIT_MS = int(os.environ.get("CARD_DETAILS_MAX_WAIT_MS", "1_200"))
RIGHT_ARROW_ROLE_TIMEOUT_MS = int(os.environ.get("RIGHT_ARROW_ROLE_TIMEOUT_MS", "100"))
DEFAULT_TIMEOUT_MS = 12_000
RARITY_PATTERN = re.compile(r"^(L|UR|SR|R|PC|C)$", re.IGNORECASE)
CARD_COUNTER_PATTERN = re.compile(r"Carte\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
COUNTER_VALUE_PATTERN = re.compile(r"^\d+\s*/\s*\d+$")
RARITY_EMOJIS = {
    "L": "👑",
    "UR": "🏆",
    "SR": "💗",
    "R": "💜",
    "PC": "🔵",
    "C": "⚪",
    "unknown": "❔",
}
RARITY_RANK = {
    "L": 0,
    "UR": 1,
    "SR": 2,
    "R": 3,
    "PC": 4,
    "C": 5,
    "unknown": 99,
}


@dataclass(frozen=True)
class CardRecord:
    """One card revealed while opening a pack."""

    pack: int
    card: int | None
    total: int | None
    name: str
    rarity: str


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_first_visible(candidates: Iterable[Locator], timeout_ms: int = 1_500) -> Locator | None:
    """Return the first locator that becomes visible from a fallback list."""

    for locator in candidates:
        candidate = locator.first
        try:
            candidate.wait_for(state="visible", timeout=timeout_ms)
            return candidate
        except PlaywrightTimeoutError:
            continue
        except PlaywrightError:
            continue
    return None


def save_failure_artifacts(page: Page | None, reason: str) -> None:
    """Write a small metadata file and screenshot when browser automation fails."""

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "-", reason).strip("-")[:60] or "failure"

    metadata = ARTIFACT_DIR / f"{safe_reason}.txt"
    metadata.write_text(
        "\n".join(
            [
                f"reason={reason}",
                f"timestamp={datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                f"url={page.url if page else 'unknown'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    if page is None:
        return

    try:
        page.screenshot(path=ARTIFACT_DIR / f"{safe_reason}.png", full_page=True)
    except PlaywrightError as exc:
        log(f"Could not capture failure screenshot: {exc}")


def settle_page(page: Page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass


def wait_for_login_hydration(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(500)


def fill_login_input(locator: Locator, value: str) -> None:
    locator.click()
    locator.press("ControlOrMeta+A")
    locator.press("Backspace")
    locator.type(value, delay=5)
    try:
        if locator.input_value(timeout=1_000) != value:
            locator.fill(value)
    except PlaywrightError:
        locator.fill(value)


def read_login_error(page: Page) -> str:
    try:
        body_text = page.locator("body").inner_text(timeout=2_000)
    except PlaywrightError:
        return ""

    normalized = re.sub(r"\s+", " ", body_text).lower()
    known_errors = (
        "missing email or phone",
        "invalid login credentials",
        "email not confirmed",
        "too many requests",
        "rate limit",
    )
    for error in known_errors:
        if error in normalized:
            return error
    return ""


def login_if_needed(page: Page, email: str, password: str) -> None:
    """Authenticate if the pulls page redirects to the WikiMasters login form."""

    settle_page(page)

    if "/login" not in page.url and get_first_visible([page.get_by_text("Ouvrir", exact=True)], 1_000):
        log("Already authenticated on pulls page.")
        return

    email_input = get_first_visible(
        [
            page.get_by_label(re.compile("adresse.*courriel", re.IGNORECASE)),
            page.get_by_placeholder(re.compile("adresse.*courriel|courriel|email", re.IGNORECASE)),
            page.locator('input[type="email"]'),
            page.locator('input[name*="email"], input[name*="mail"]'),
        ],
        timeout_ms=3_000,
    )
    password_input = get_first_visible(
        [
            page.get_by_label(re.compile("mot de passe|password", re.IGNORECASE)),
            page.get_by_placeholder(re.compile("mot de passe|password", re.IGNORECASE)),
            page.locator('input[type="password"]'),
        ],
        timeout_ms=3_000,
    )

    if email_input is None or password_input is None:
        if "/login" in page.url:
            raise RuntimeError("Login page is visible, but email/password fields were not found.")
        log("No login form found; assuming existing authenticated session.")
        return

    log("Logging in to WikiMasters.")
    email_input.fill(email)
    password_input.fill(password)

    submit = get_first_visible(
        [
            page.get_by_role("button", name=re.compile("^connexion$", re.IGNORECASE)),
            page.get_by_text("Connexion", exact=True),
            page.locator('button[type="submit"]'),
        ],
        timeout_ms=3_000,
    )
    if submit is None:
        raise RuntimeError("Could not find the Connexion button.")

    login_error = ""
    for attempt in range(1, 3):
        wait_for_login_hydration(page)
        fill_login_input(email_input, email)
        fill_login_input(password_input, password)
        page.wait_for_timeout(250)

        submit.click()
        try:
            page.wait_for_url(re.compile(r".*/(pulls|paquets|collection|profile|profil).*"), timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            settle_page(page)

        if "/login" not in page.url:
            break

        login_error = read_login_error(page)
        if attempt == 1 and login_error == "missing email or phone":
            log("Login form submitted before app state was ready; retrying once.")
            continue
        raise RuntimeError(
            "Login did not complete; still on the login page"
            + (f" ({login_error})." if login_error else ".")
        )

    log("Login completed.")


def click_known_dialog_close_by_geometry(page: Page) -> bool:
    """Close the current contest/promo dialog when it has no accessible label."""

    coords = page.evaluate(
        """
        () => {
          const keywords = /concours de collections|nouveau concours|composez une collection/i;
          const candidates = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')]
            .map((element) => {
              const rect = element.getBoundingClientRect();
              const style = window.getComputedStyle(element);
              const text = (element.innerText || '').trim();

              if (
                !keywords.test(text) ||
                style.visibility === 'hidden' ||
                style.display === 'none' ||
                rect.width < 240 ||
                rect.height < 160 ||
                rect.width > window.innerWidth * 0.90 ||
                rect.height > window.innerHeight * 0.90
              ) {
                return null;
              }

              const centerPenalty =
                Math.abs(rect.left + rect.width / 2 - window.innerWidth / 2) +
                Math.abs(rect.top + rect.height / 2 - window.innerHeight / 2);
              return { element, rect, score: text.length - centerPenalty };
            })
            .filter(Boolean)
            .sort((a, b) => b.score - a.score);

          const dialog = candidates[0];
          if (!dialog) return null;

          const controls = [...dialog.element.querySelectorAll('button, [role="button"], a')]
            .map((element) => {
              const rect = element.getBoundingClientRect();
              const style = window.getComputedStyle(element);
              const text = [
                element.innerText,
                element.getAttribute('aria-label'),
                element.getAttribute('title'),
              ].join(' ').trim().toLowerCase();

              if (
                style.visibility === 'hidden' ||
                style.display === 'none' ||
                rect.width < 16 ||
                rect.height < 16
              ) {
                return null;
              }

              const explicitClose = /plus tard|fermer|close|×/.test(text);
              const topRightClose =
                rect.top < dialog.rect.top + 72 &&
                rect.left > dialog.rect.right - 96;
              if (!explicitClose && !topRightClose) {
                return null;
              }

              const score =
                (explicitClose ? 1000 : 0) +
                (topRightClose ? 500 : 0) -
                Math.abs(rect.left + rect.width / 2 - dialog.rect.right);

              return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, score };
            })
            .filter(Boolean)
            .sort((a, b) => b.score - a.score);

          return controls[0] || null;
        }
        """
    )

    if not coords:
        return False

    page.mouse.click(coords["x"], coords["y"])
    return True


def dismiss_known_blocking_dialogs(page: Page) -> None:
    """Best-effort dismissal for dialogs that cover the pulls page."""

    dismissed = False
    for _ in range(2):
        close_control = get_first_visible(
            [
                page.get_by_role("button", name=re.compile("^plus tard$", re.IGNORECASE)),
                page.locator(
                    'button:has-text("Plus tard"), '
                    '[role="button"]:has-text("Plus tard"), '
                    'a:has-text("Plus tard")'
                ),
                page.get_by_text("Plus tard", exact=True),
            ],
            timeout_ms=500,
        )

        try:
            if close_control is not None:
                close_control.click(timeout=1_000)
            elif click_known_dialog_close_by_geometry(page):
                pass
            else:
                break
        except PlaywrightError:
            break

        dismissed = True
        page.wait_for_timeout(300)

    if dismissed:
        log("Dismissed blocking dialog on pulls page.")


def find_open_button(page: Page) -> Locator | None:
    return get_first_visible(
        [
            page.get_by_role("button", name=re.compile("^ouvrir$", re.IGNORECASE)),
            page.locator(
                'button:has-text("Ouvrir"), '
                '[role="button"]:has-text("Ouvrir"), '
                'a:has-text("Ouvrir")'
            ),
            page.get_by_text("Ouvrir", exact=True),
        ],
        timeout_ms=2_000,
    )


def read_pack_status(page: Page) -> str:
    try:
        body_text = page.locator("body").inner_text(timeout=2_000)
    except PlaywrightError:
        return "unknown"

    match = re.search(r"(\d+)\s*/\s*(\d+)\s+paquets?\s+disponibles?", body_text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}/{match.group(2)} packs available"

    if re.search(r"aucun|0\s*/\s*\d+\s+paquets?", body_text, re.IGNORECASE):
        return "no packs available"

    return "pack status unknown"


def read_card_counter(page: Page) -> tuple[int, int] | None:
    """Read the current 'Carte X / Y' counter from the page body."""

    try:
        body_text = page.locator("body").inner_text(timeout=2_000)
    except PlaywrightError:
        return None

    match = CARD_COUNTER_PATTERN.search(body_text)
    if not match:
        return None

    return int(match.group(1)), int(match.group(2))


def normalize_visible_lines(text: str) -> list[str]:
    """Split browser text into non-empty, whitespace-normalized lines."""

    lines = []
    for line in text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return lines


def is_card_title_candidate(line: str) -> bool:
    """Filter out common UI labels and counters while parsing card text."""

    normalized = line.lower()
    if RARITY_PATTERN.fullmatch(line):
        return False
    if CARD_COUNTER_PATTERN.search(line):
        return False
    if COUNTER_VALUE_PATTERN.fullmatch(line):
        return False
    if re.fullmatch(r"[\d\s]+", line):
        return False
    if re.search(r"paquets?\s+disponibles?|prochain dans|encore \d+ cartes?", line, re.IGNORECASE):
        return False
    if normalized in {
        "carte",
        "wikimasters",
        "paquets",
        "collection",
        "echanges",
        "échanges",
        "marche",
        "marché",
        "profil",
        "toutes les cartes",
        "guilde",
        "amis",
        "messages",
        "bataille",
        "succès",
        "succes",
        "classement",
        "paramètres",
        "parametres",
        "ouvrir",
        "comment ça marche ?",
        "comment ca marche ?",
    }:
        return False
    return True


def parse_card_from_text(text: str, counter: tuple[int, int] | None) -> tuple[str, str]:
    """Extract the best card title and rarity from a card/modal text block."""

    lines = normalize_visible_lines(text)
    start_index = 0
    for index, line in enumerate(lines):
        if CARD_COUNTER_PATTERN.search(line):
            start_index = index + 1
            break
        if line.lower() == "carte" and index + 1 < len(lines) and COUNTER_VALUE_PATTERN.fullmatch(lines[index + 1]):
            start_index = index + 2
            break

    candidate_lines = lines[start_index : start_index + 24]
    rarity = "unknown"
    rarity_index: int | None = None
    name = "unknown"

    for index, line in enumerate(candidate_lines):
        rarity_match = RARITY_PATTERN.fullmatch(line)
        if rarity_match:
            rarity = rarity_match.group(1).upper()
            rarity_index = index
            break

        inline_match = re.match(r"^(L|UR|SR|R|PC|C)\s+(.+)$", line, re.IGNORECASE)
        if inline_match:
            rarity = inline_match.group(1).upper()
            possible_name = inline_match.group(2).strip()
            if is_card_title_candidate(possible_name):
                name = possible_name
            return name, rarity

    if rarity_index is not None:
        for line in candidate_lines[rarity_index + 1 :]:
            if is_card_title_candidate(line):
                name = line
                break

        if name == "unknown":
            for line in reversed(candidate_lines[:rarity_index]):
                if is_card_title_candidate(line):
                    name = line
                    break
    else:
        for line in candidate_lines:
            if is_card_title_candidate(line):
                name = line
                break

    return name, rarity


def read_card_area_text(page: Page) -> str:
    """Sample the modal/card area and return the most card-like visible text."""

    try:
        return page.evaluate(
            """
            () => {
              const samples = [
                [0.58, 0.38],
                [0.58, 0.48],
                [0.58, 0.58],
                [0.58, 0.68],
                [0.52, 0.55],
                [0.64, 0.55],
              ];
              const rarityPattern = /^(L|UR|SR|R|PC|C)$/i;
              const candidates = [];

              // The reveal modal has no stable card selector, so sample points
              // near the expected card area and score parent nodes by rarity.
              for (const [xRatio, yRatio] of samples) {
                let element = document.elementFromPoint(
                  window.innerWidth * xRatio,
                  window.innerHeight * yRatio
                );

                for (let depth = 0; element && depth < 8; depth += 1) {
                  const rect = element.getBoundingClientRect();
                  const style = window.getComputedStyle(element);
                  const text = (element.innerText || '').trim();
                  const lines = text.split('\\n').map((line) => line.trim()).filter(Boolean);
                  const hasRarity = lines.some((line) => rarityPattern.test(line));

                  if (
                    text &&
                    style.visibility !== 'hidden' &&
                    style.display !== 'none' &&
                    rect.left > window.innerWidth * 0.25 &&
                    rect.width >= 160 &&
                    rect.height >= 160 &&
                    rect.width <= 760 &&
                    rect.height <= 840 &&
                    lines.length >= 2
                  ) {
                    const cardCenterX = rect.left + rect.width / 2;
                    const cardCenterY = rect.top + rect.height / 2;
                    const centerPenalty =
                      Math.abs(cardCenterX - window.innerWidth * 0.58) +
                      Math.abs(cardCenterY - window.innerHeight * 0.54);
                    const score =
                      (hasRarity ? 10000 : 0) +
                      Math.min(lines.length, 8) * 100 -
                      centerPenalty;
                    candidates.push({ text, score });
                  }

                  element = element.parentElement;
                }
              }

              candidates.sort((a, b) => b.score - a.score);
              return candidates[0]?.text || '';
            }
            """
        )
    except PlaywrightError:
        return ""


def display_rarity(rarity: str) -> str:
    normalized = rarity.upper() if rarity != "unknown" else "unknown"
    emoji = RARITY_EMOJIS.get(normalized, RARITY_EMOJIS["unknown"])
    return f"{emoji} {normalized}"


def rarity_rank(rarity: str) -> int:
    normalized = rarity.upper() if rarity != "unknown" else "unknown"
    return RARITY_RANK.get(normalized, RARITY_RANK["unknown"])


def needs_card_details_retry(record: CardRecord) -> bool:
    return record.name == "unknown" or record.name.lower() == "carte" or record.rarity == "unknown"


def read_visible_card(page: Page, pack_index: int, counter: tuple[int, int] | None) -> CardRecord:
    """Read the currently shown card, falling back to body text if needed."""

    card_area_text = read_card_area_text(page)
    name, rarity = parse_card_from_text(card_area_text, counter)

    if name == "unknown" or rarity == "unknown":
        try:
            body_text = page.locator("body").inner_text(timeout=2_000)
        except PlaywrightError:
            body_text = ""

        fallback_name, fallback_rarity = parse_card_from_text(body_text, counter)
        if name == "unknown":
            name = fallback_name
        if rarity == "unknown":
            rarity = fallback_rarity

    return CardRecord(
        pack=pack_index,
        card=counter[0] if counter else None,
        total=counter[1] if counter else None,
        name=name,
        rarity=rarity,
    )


def wait_for_visible_card(page: Page, pack_index: int, counter: tuple[int, int] | None) -> CardRecord:
    deadline = datetime.now(timezone.utc).timestamp() + CARD_DETAILS_MAX_WAIT_MS / 1_000
    record = read_visible_card(page, pack_index, counter)
    while needs_card_details_retry(record) and datetime.now(timezone.utc).timestamp() < deadline:
        page.wait_for_timeout(120)
        record = read_visible_card(page, pack_index, counter)
    return record


def log_visible_card(
    page: Page,
    pack_index: int,
    counter: tuple[int, int] | None,
    records: list[CardRecord],
    seen_card_numbers: set[int],
) -> None:
    if counter and counter[0] in seen_card_numbers:
        return
    if counter:
        seen_card_numbers.add(counter[0])
    elif -1 in seen_card_numbers:
        return
    else:
        seen_card_numbers.add(-1)

    record = wait_for_visible_card(page, pack_index, counter)
    records.append(record)
    log(f"Opened card: {record.name} ; rarity={display_rarity(record.rarity)}")


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def write_card_summary(records: list[CardRecord]) -> None:
    """Append opened cards to the GitHub Actions step summary when available."""

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = ["", "## WikiMasters Cards Opened", ""]
    if not records:
        lines.append("No cards were opened in this run.")
    else:
        lines.extend(
            [
                "| Name | Rarity |",
                "|---|---|",
            ]
        )
        sorted_records = [
            record
            for _, record in sorted(
                enumerate(records),
                key=lambda item: (rarity_rank(item[1].rarity), item[0]),
            )
        ]
        for record in sorted_records:
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(record.name),
                        markdown_cell(display_rarity(record.rarity)),
                    ]
                )
                + " |"
            )

    try:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write("\n".join(lines) + "\n")
    except OSError as exc:
        log(f"Could not write GitHub step summary: {exc}")


def wait_for_card_counter(page: Page, timeout_ms: int = PACK_OPEN_MAX_WAIT_MS) -> tuple[int, int] | None:
    deadline = datetime.now(timezone.utc).timestamp() + timeout_ms / 1_000
    while datetime.now(timezone.utc).timestamp() < deadline:
        counter = read_card_counter(page)
        if counter:
            return counter
        page.wait_for_timeout(150)
    return read_card_counter(page)


def click_right_arrow_by_role(page: Page) -> bool:
    locator = get_first_visible(
        [
            page.get_by_role("button", name=re.compile("suivant|next|droite|right|→|›", re.IGNORECASE)),
            page.get_by_label(re.compile("suivant|next|droite|right", re.IGNORECASE)),
        ],
        timeout_ms=RIGHT_ARROW_ROLE_TIMEOUT_MS,
    )
    if locator is None:
        return False

    try:
        if locator.is_enabled(timeout=RIGHT_ARROW_ROLE_TIMEOUT_MS):
            locator.click(timeout=1_000)
            return True
    except PlaywrightError:
        return False

    return False


def click_right_arrow_by_geometry(page: Page) -> bool:
    """Click the modal next arrow by scoring likely controls near the card."""

    coords = page.evaluate(
        """
        () => {
          const candidates = [...document.querySelectorAll('button, [role="button"], a')];
          const viewportWidth = window.innerWidth;
          const viewportHeight = window.innerHeight;

          const scored = candidates
            .map((element) => {
              const rect = element.getBoundingClientRect();
              const style = window.getComputedStyle(element);
              const centerX = rect.left + rect.width / 2;
              const centerY = rect.top + rect.height / 2;
              const disabled = element.disabled ||
                element.getAttribute('aria-disabled') === 'true' ||
                style.pointerEvents === 'none';

              if (
                disabled ||
                style.visibility === 'hidden' ||
                style.display === 'none' ||
                rect.width < 28 ||
                rect.height < 28 ||
                rect.width > 120 ||
                rect.height > 120 ||
                centerX < viewportWidth * 0.50 ||
                centerY < viewportHeight * 0.50 ||
                centerY > viewportHeight * 0.84
              ) {
                return null;
              }

              const aria = (element.getAttribute('aria-label') || '').toLowerCase();
              const title = (element.getAttribute('title') || '').toLowerCase();
              const text = (element.innerText || '').trim().toLowerCase();
              const explicitNext = /suivant|next|droite|right|→|›/.test(`${aria} ${title} ${text}`);
              const roundish = Math.abs(rect.width - rect.height) < 20;
              const score =
                (explicitNext ? 1000 : 0) +
                (roundish ? 100 : 0) +
                centerX -
                Math.abs(centerY - viewportHeight * 0.70);

              return { x: centerX, y: centerY, score };
            })
            .filter(Boolean)
            .sort((a, b) => b.score - a.score);

          return scored[0] || null;
        }
        """
    )

    if not coords:
        return False

    page.mouse.click(coords["x"], coords["y"])
    return True


def click_right_arrow(page: Page) -> bool:
    return click_right_arrow_by_geometry(page) or click_right_arrow_by_role(page)


def reveal_current_pack(page: Page, pack_index: int, records: list[CardRecord]) -> None:
    """Walk through all visible cards in the current opened pack."""

    counter = wait_for_card_counter(page)
    if counter:
        log(f"Pack opened. Card {counter[0]}/{counter[1]}.")
    else:
        log("Pack opened. Card counter not found; using bounded arrow navigation.")

    stagnant_clicks = 0
    previous_counter = counter
    seen_card_numbers: set[int] = set()
    log_visible_card(page, pack_index, counter, records, seen_card_numbers)

    for advance in range(MAX_CARD_ADVANCES):
        counter = read_card_counter(page)
        log_visible_card(page, pack_index, counter, records, seen_card_numbers)
        if counter and counter[0] >= counter[1]:
            log(f"Finished pack at card {counter[0]}/{counter[1]}.")
            return

        clicked = click_right_arrow(page)
        if not clicked:
            log("No clickable right arrow found; assuming pack is finished.")
            return

        page.wait_for_timeout(CARD_ADVANCE_DELAY_MS)
        current_counter = read_card_counter(page)
        if current_counter:
            log(f"Advanced to card {current_counter[0]}/{current_counter[1]}.")
        log_visible_card(page, pack_index, current_counter, records, seen_card_numbers)

        if current_counter == previous_counter:
            stagnant_clicks += 1
        else:
            stagnant_clicks = 0

        previous_counter = current_counter
        if stagnant_clicks >= 2:
            log("Card counter stopped changing; stopping this pack.")
            return

    log(f"Reached MAX_CARD_ADVANCES={MAX_CARD_ADVANCES}; stopping this pack.")


def open_all_available_packs(page: Page, records: list[CardRecord]) -> int:
    """Open packs from the pulls page until none are available or limit is hit."""

    opened = 0

    for pack_index in range(1, MAX_PACKS_PER_RUN + 1):
        page.goto(PULLS_URL, wait_until="domcontentloaded")
        settle_page(page)
        dismiss_known_blocking_dialogs(page)

        open_button = find_open_button(page)
        if open_button is None:
            log(f"No open button found ({read_pack_status(page)}).")
            return opened

        try:
            if not open_button.is_enabled(timeout=1_000):
                log(f"Open button is disabled ({read_pack_status(page)}).")
                return opened
        except PlaywrightError:
            pass

        log(f"Opening pack {pack_index} ({read_pack_status(page)}).")
        try:
            open_button.click(timeout=5_000)
        except PlaywrightError:
            dismiss_known_blocking_dialogs(page)
            open_button = find_open_button(page)
            if open_button is None:
                raise
            open_button.click(timeout=5_000)

        opened += 1
        reveal_current_pack(page, pack_index, records)

    log(f"Reached MAX_PACKS_PER_RUN={MAX_PACKS_PER_RUN}; stopping run.")
    return opened


def main() -> int:
    """Run the browser session, collect card records, and handle failures."""

    email = required_env("WIKIMASTERS_EMAIL")
    password = required_env("WIKIMASTERS_PASSWORD")
    headless = os.environ.get("HEADLESS", "1").lower() not in {"0", "false", "no"}
    records: list[CardRecord] = []

    page: Page | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        try:
            page.goto(PULLS_URL, wait_until="domcontentloaded")
            login_if_needed(page, email, password)
            opened = open_all_available_packs(page, records)
            write_card_summary(records)
            log(f"Run completed successfully. Packs opened: {opened}.")
            return 0
        except Exception as exc:
            log(f"Run failed: {exc}")
            write_card_summary(records)
            save_failure_artifacts(page, "wikimasters-open-packs-failure")
            return 1
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
