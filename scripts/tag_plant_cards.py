#!/usr/bin/env python3
"""Tag WikiMasters collection cards for actual plant taxa with an etiquette."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from scripts.env_loader import load_env_file
except ModuleNotFoundError:
    from env_loader import load_env_file

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
        Locator,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError:  # Allows pure classifier tests without Playwright installed.
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    Locator = Any
    Page = Any
    sync_playwright = None


load_env_file(Path(os.environ.get("ENV_FILE", Path(__file__).resolve().parents[1] / ".env")))

BASE_URL = "https://www.wiki-masters.com"
COLLECTION_URL = f"{BASE_URL}/collection"
WIKIPEDIA_API_URL = "https://fr.wikipedia.org/w/api.php"
ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", "artifacts"))
DEFAULT_CACHE_PATH = ARTIFACT_DIR / "plant_wikipedia_cache.json"
DEFAULT_REPORT_PATH = ARTIFACT_DIR / "plant_tag_report.md"
DEFAULT_TIMEOUT_MS = 12_000
RARITY_PATTERN = re.compile(r"^(L|UR|SR|R|PC|C)$", re.IGNORECASE)
PAGE_COUNTER_PATTERN = re.compile(r"Page\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
TARGET_TAG = "plante"

PLANT_TAXON_PHRASES = (
    "genre de plantes",
    "genre de plante",
    "espece de plantes",
    "espece de plante",
    "espece d'arbre",
    "espece d'arbres",
    "espece d'orchidee",
    "espece d'orchidees",
    "espece de cactus",
    "espece de plante a fleur",
    "espece de plantes a fleurs",
    "famille de plantes",
    "sous-famille de plantes",
    "tribu de plantes",
    "famille botanique",
    "taxon vegetal",
    "regne vegetal",
    "plante a fleurs",
    "plante herbacee",
    "plante grimpante",
    "plante cultivee",
    "graminee",
    "poaceae",
    "fabaceae",
    "rosaceae",
    "asteraceae",
    "orchidee",
    "fougere",
    "bryophyte",
    "angiosperme",
    "gymnosperme",
)
SUPPORTING_PLANT_CONTEXT_PHRASES = (
    "agriculture",
    "agricole",
    "horticulture",
    "jardinage",
    "sylviculture",
    "foret",
    "forets",
    "culture agricole",
    "semence",
    "semences",
    "graine",
    "graines",
    "feuille",
    "feuilles",
    "racine",
    "racines",
    "tige",
    "tiges",
    "bois",
    "verger",
    "vigne",
    "viticulture",
)
PLANT_CATEGORY_SUPPORT_PHRASES = (
    "plante",
    "plantes",
    "plante a fleurs",
    "vegetal",
    "vegetaux",
    "flore",
    "orchidee",
    "orchidees",
    "cactus",
    "poaceae",
    "fabaceae",
    "rosaceae",
    "asteraceae",
    "conifere",
    "fougere",
    "angiosperme",
    "gymnosperme",
)
NEGATIVE_CONTEXT_PHRASES = (
    "film",
    "acteur",
    "actrice",
    "ecrivain",
    "ecrivaine",
    "roman",
    "livre",
    "chanson",
    "album",
    "groupe de rock",
    "homme politique",
    "femme politique",
    "commune",
    "ville",
    "district",
    "barrage",
    "election",
    "roi",
    "reine",
    "football",
    "mathematique",
    "maladie",
    "syndrome",
    "champignon",
    "souche",
    "entreprise",
    "societe",
    "langue",
    "pays",
    "guerre",
    "bataille",
)


@dataclass(frozen=True)
class CardRecord:
    key: str
    title: str
    subtitle: str
    rarity: str
    tags: tuple[str, ...]
    visible_text: str


@dataclass(frozen=True)
class WikipediaMetadata:
    title: str
    description: str = ""
    extract: str = ""
    categories: tuple[str, ...] = ()
    missing: bool = False


@dataclass(frozen=True)
class PlantClassification:
    is_plant_related: bool
    score: int
    reason: str


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    ascii_text = ascii_text.lower().replace("’", "'")
    ascii_text = re.sub(r"[^a-z0-9'/ -]+", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def phrase_matches(text: str, phrases: Sequence[str]) -> list[str]:
    normalized = normalize_text(text)
    matches: list[str] = []
    for phrase in phrases:
        normalized_phrase = normalize_text(phrase)
        if re.search(rf"(?<![a-z0-9]){re.escape(normalized_phrase)}(?![a-z0-9])", normalized):
            matches.append(phrase)
    return matches


def looks_like_latin_taxon(value: str) -> bool:
    normalized = value.strip()
    return bool(re.fullmatch(r"[A-Z][a-z-]+(?:\s+[a-z-]+){1,2}", normalized))


def normalized_tag(value: str) -> str:
    return normalize_text(value).replace(" ", "-")


def has_tag(card: CardRecord, target_tag: str) -> bool:
    wanted = normalized_tag(target_tag)
    return any(normalized_tag(tag) == wanted for tag in card.tags)


def has_target_tag(card: CardRecord, target_tag: str) -> bool:
    wanted = normalized_tag(target_tag)
    visible_lines = [normalized_tag(line) for line in card.visible_text.splitlines()]
    return has_tag(card, target_tag) or wanted in visible_lines


def looks_like_stat_line(line: str) -> bool:
    normalized = line.strip()
    if not normalized:
        return True
    return bool(re.fullmatch(r"[^\w]*\d[\d\s.,]*[^\w]*", normalized))


def looks_like_tag_line(line: str) -> bool:
    normalized = normalize_text(line)
    if not normalized or looks_like_stat_line(line):
        return False
    if RARITY_PATTERN.fullmatch(line):
        return False
    if len(line) > 32:
        return False
    if re.search(r"[.!?;:]", line):
        return False
    if "/" in line:
        return True
    return len(normalized.split()) <= 2


def parse_card_lines(lines: Sequence[str]) -> CardRecord | None:
    clean_lines = [re.sub(r"\s+", " ", line).strip() for line in lines if line and line.strip()]
    rarity_index = next((index for index, line in enumerate(clean_lines) if RARITY_PATTERN.fullmatch(line)), -1)
    if rarity_index < 0:
        return None

    rarity = clean_lines[rarity_index].upper()
    title = ""
    subtitle = ""
    tags: list[str] = []
    payload = clean_lines[rarity_index + 1 :]

    for line in payload:
        if looks_like_stat_line(line) or RARITY_PATTERN.fullmatch(line):
            continue
        if not title:
            title = line
            continue
        if not subtitle:
            subtitle = line
            continue
        if looks_like_tag_line(line):
            tags.append(line)

    if not title:
        return None

    visible_text = "\n".join(clean_lines)
    key_material = "\0".join([title, subtitle, rarity])
    key = hashlib.sha1(key_material.encode("utf-8")).hexdigest()[:16]
    return CardRecord(
        key=key,
        title=title,
        subtitle=subtitle,
        rarity=rarity,
        tags=tuple(dict.fromkeys(tags)),
        visible_text=visible_text,
    )


def classify_plant_card(card: CardRecord, metadata: WikipediaMetadata | None = None) -> PlantClassification:
    visible_text = f"{card.title} {card.subtitle} {' '.join(card.tags)}"
    category_text = ""
    description_text = ""
    extract_text = ""
    if metadata and not metadata.missing:
        category_text = " ".join(metadata.categories)
        description_text = metadata.description
        extract_text = metadata.extract

    score = 0
    reasons: list[str] = []

    visible_taxon = phrase_matches(visible_text, PLANT_TAXON_PHRASES)
    category_taxon = phrase_matches(category_text, PLANT_TAXON_PHRASES)
    category_support = phrase_matches(category_text, PLANT_CATEGORY_SUPPORT_PHRASES)
    description_taxon = phrase_matches(description_text, PLANT_TAXON_PHRASES)
    extract_taxon = phrase_matches(extract_text, PLANT_TAXON_PHRASES)
    supporting_context = phrase_matches(" ".join([visible_text, description_text, category_text]), SUPPORTING_PLANT_CONTEXT_PHRASES)
    visible_negative = phrase_matches(visible_text, NEGATIVE_CONTEXT_PHRASES)
    metadata_negative = phrase_matches(
        " ".join([metadata.description, card.subtitle]) if metadata else card.subtitle,
        NEGATIVE_CONTEXT_PHRASES,
    )

    if visible_taxon:
        score += 10
        reasons.append(f"visible plant taxon term: {visible_taxon[0]}")
    if description_taxon:
        score += 8
        reasons.append(f"Wikipedia description plant taxon term: {description_taxon[0]}")
    if category_taxon:
        score += 6
        reasons.append(f"Wikipedia category plant taxon term: {category_taxon[0]}")
    if extract_taxon:
        score += 2
        reasons.append(f"Wikipedia extract plant taxon term: {extract_taxon[0]}")
    if category_support and (extract_taxon or looks_like_latin_taxon(card.title)):
        score += 2
        reasons.append(f"Wikipedia category plant support: {category_support[0]}")
    if supporting_context and score > 0:
        score += 1
        reasons.append(f"supporting plant context: {supporting_context[0]}")

    if visible_negative:
        score -= 8
        reasons.append(f"visible non-plant context: {visible_negative[0]}")
    if metadata_negative:
        score -= 6
        reasons.append(f"metadata non-plant context: {metadata_negative[0]}")

    has_taxon_evidence = bool(visible_taxon or description_taxon or category_taxon or extract_taxon)
    is_plant_related = has_taxon_evidence and score >= 4
    reason = "; ".join(dict.fromkeys(reasons)) or "no plant evidence found"
    return PlantClassification(is_plant_related=is_plant_related, score=score, reason=reason)


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def get_first_visible(candidates: Iterable[Locator], timeout_ms: int = 1_500) -> Locator | None:
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

    normalized = normalize_text(body_text)
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
    settle_page(page)
    if "/login" not in page.url:
        email_field = get_first_visible([page.locator('input[type="email"]')], timeout_ms=500)
        if email_field is None:
            log("Already authenticated.")
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


def block_heavy_resources(route: Any) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        route.abort()
        return
    route.continue_()


def extract_visible_cards(page: Page) -> list[CardRecord]:
    raw_cards = page.evaluate(
        """
        () => {
          const rarityPattern = /^(L|UR|SR|R|PC|C)$/i;
          const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const linesFor = (element) =>
            (element.innerText || '').split('\\n').map(normalize).filter(Boolean);
          const isVisible = (element, rect) => {
            const style = window.getComputedStyle(element);
            return (
              style.visibility !== 'hidden' &&
              style.display !== 'none' &&
              style.opacity !== '0' &&
              rect.width >= 110 &&
              rect.width <= 280 &&
              rect.height >= 150 &&
              rect.height <= 380 &&
              rect.bottom >= 0 &&
              rect.top <= window.innerHeight &&
              rect.right >= 0 &&
              rect.left <= window.innerWidth
            );
          };
          const selector = 'article, a, button, [role="button"], [role="listitem"], div';
          const candidates = [];
          const seen = new Set();

          for (const element of document.querySelectorAll(selector)) {
            const rect = element.getBoundingClientRect();
            if (!isVisible(element, rect)) continue;
            const lines = linesFor(element);
            const rarityIndex = lines.findIndex((line) => rarityPattern.test(line));
            if (rarityIndex < 0 || lines.length < rarityIndex + 3) continue;
            const text = lines.join('\\n');
            if (/sélectionner|selectionner|collection|toutes les étiquettes|rarete/i.test(text) && lines.length > 12) {
              continue;
            }

            const key = lines.slice(rarityIndex, rarityIndex + 5).join('|').toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            candidates.push({
              lines,
              top: rect.top,
              left: rect.left,
              area: rect.width * rect.height,
            });
          }

          candidates.sort((a, b) => a.top - b.top || a.left - b.left || b.area - a.area);
          return candidates.map((candidate) => candidate.lines);
        }
        """
    )

    cards: list[CardRecord] = []
    seen_keys: set[str] = set()
    for lines in raw_cards:
        card = parse_card_lines(lines)
        if card and card.key not in seen_keys:
            cards.append(card)
            seen_keys.add(card.key)
    return cards


COLLECTION_SCROLL_TARGET_JS = """
() => {
  const elements = [...document.querySelectorAll('main, section, div')];
  const scrollables = elements
    .map((element) => {
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      const overflowY = style.overflowY || '';
      const canScroll = element.scrollHeight > element.clientHeight + 20;
      if (!canScroll || !/(auto|scroll|overlay)/.test(overflowY)) return null;

      const text = (element.innerText || '').toLowerCase();
      const collectionScore = text.includes('collection') ? 1000 : 0;
      const sizeScore = Math.min(element.clientHeight, window.innerHeight);
      const overflowScore = element.scrollHeight - element.clientHeight;
      return {
        element,
        score: collectionScore + sizeScore + overflowScore / 10,
        top: rect.top,
        left: rect.left,
      };
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left);

  return scrollables[0]?.element || document.scrollingElement || document.documentElement;
}
"""


def read_collection_scroll_metrics(page: Page) -> dict[str, int | bool]:
    return page.evaluate(
        f"""
        () => {{
          const target = ({COLLECTION_SCROLL_TARGET_JS})();
          return {{
            scrollTop: Math.round(target.scrollTop),
            clientHeight: Math.round(target.clientHeight),
            scrollHeight: Math.round(target.scrollHeight),
            atBottom: target.scrollTop + target.clientHeight >= target.scrollHeight - 24,
          }};
        }}
        """
    )


def scroll_collection(page: Page) -> dict[str, int | bool]:
    return page.evaluate(
        f"""
        () => {{
          const target = ({COLLECTION_SCROLL_TARGET_JS})();
          target.scrollBy(0, Math.max(240, Math.floor(target.clientHeight * 0.82)));
          return {{
            scrollTop: Math.round(target.scrollTop),
            clientHeight: Math.round(target.clientHeight),
            scrollHeight: Math.round(target.scrollHeight),
            atBottom: target.scrollTop + target.clientHeight >= target.scrollHeight - 24,
          }};
        }}
        """
    )


def reset_collection_scroll(page: Page) -> None:
    page.evaluate(
        f"""
        () => {{
          const target = ({COLLECTION_SCROLL_TARGET_JS})();
          target.scrollTo(0, 0);
        }}
        """
    )


def read_collection_page_counter(page: Page) -> tuple[int, int] | None:
    try:
        body_text = page.locator("body").inner_text(timeout=2_000)
    except PlaywrightError:
        return None

    match = PAGE_COUNTER_PATTERN.search(body_text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def page_label(counter: tuple[int, int] | None) -> str:
    if counter is None:
        return "current page"
    return f"page {counter[0]}/{counter[1]}"


def visible_card_signature(page: Page) -> tuple[str, ...]:
    return tuple(card.key for card in extract_visible_cards(page)[:6])


def find_next_page_button(page: Page) -> Locator | None:
    return get_first_visible(
        [
            page.get_by_role("button", name=re.compile(r"suivant|next|→", re.IGNORECASE)),
            page.get_by_text(re.compile(r"suivant|next", re.IGNORECASE)),
        ],
        timeout_ms=1_500,
    )


def click_next_collection_page(page: Page) -> bool:
    next_button = find_next_page_button(page)
    if next_button is None:
        return False

    try:
        if not next_button.is_enabled(timeout=500):
            return False
    except PlaywrightError:
        pass

    previous_counter = read_collection_page_counter(page)
    previous_signature = visible_card_signature(page)
    next_button.click(timeout=5_000)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        counter = read_collection_page_counter(page)
        signature = visible_card_signature(page)
        if previous_counter and counter and counter[0] != previous_counter[0]:
            reset_collection_scroll(page)
            page.wait_for_timeout(300)
            return True
        if not previous_counter and signature and signature != previous_signature:
            reset_collection_scroll(page)
            page.wait_for_timeout(300)
            return True

    return False


def scan_collection_page(page: Page, max_cards: int, scroll_delay_ms: int) -> list[CardRecord]:
    records: dict[str, CardRecord] = {}
    stagnant_rounds = 0
    initial_deadline = time.monotonic() + 15

    reset_collection_scroll(page)
    page.wait_for_timeout(scroll_delay_ms)

    while time.monotonic() < initial_deadline:
        initial_cards = extract_visible_cards(page)
        if initial_cards:
            for card in initial_cards:
                records.setdefault(card.key, card)
            break
        page.wait_for_timeout(500)

    while True:
        before_count = len(records)
        for card in extract_visible_cards(page):
            records.setdefault(card.key, card)

        if len(records) != before_count:
            stagnant_rounds = 0
        else:
            stagnant_rounds += 1

        if max_cards > 0 and len(records) >= max_cards:
            break

        metrics = read_collection_scroll_metrics(page)
        if metrics["atBottom"] and stagnant_rounds >= 2:
            break

        previous_scroll_top = metrics["scrollTop"]
        metrics = scroll_collection(page)
        page.wait_for_timeout(scroll_delay_ms)
        if metrics["scrollTop"] == previous_scroll_top:
            stagnant_rounds += 1

    return list(records.values())[:max_cards or None]


def scan_collection(page: Page, max_cards: int, scroll_delay_ms: int) -> list[CardRecord]:
    records: dict[str, CardRecord] = {}
    visited_pages: set[tuple[int, int]] = set()

    while True:
        counter = read_collection_page_counter(page)
        if counter:
            if counter in visited_pages:
                log(f"Already scanned {page_label(counter)}; stopping to avoid a pagination loop.")
                break
            visited_pages.add(counter)

        remaining = max_cards - len(records) if max_cards > 0 else 0
        log(f"Scanning collection {page_label(counter)}.")
        page_cards = scan_collection_page(page, remaining, scroll_delay_ms)

        before_total = len(records)
        for card in page_cards:
            records.setdefault(card.key, card)
        added = len(records) - before_total
        log(f"Scanned {page_label(counter)}: {len(page_cards)} card(s), {added} new, {len(records)} total.")

        if max_cards > 0 and len(records) >= max_cards:
            log(f"Reached --max-cards={max_cards}; stopping scan.")
            break

        counter = read_collection_page_counter(page)
        if counter and counter[0] >= counter[1]:
            log(f"Reached final collection page {counter[0]}/{counter[1]}.")
            break

        if not click_next_collection_page(page):
            log("No enabled next page button found; collection scan is complete.")
            break

    return list(records.values())[:max_cards or None]


class WikipediaClient:
    def __init__(self, cache_path: Path, delay_ms: int, batch_size: int, timeout_seconds: int = 15) -> None:
        self.cache_path = cache_path
        self.delay_ms = delay_ms
        self.batch_size = max(1, min(batch_size, 25))
        self.timeout_seconds = timeout_seconds
        self.cache: dict[str, dict[str, Any]] = self._load_cache()
        self.description_prop_supported = True

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            log(f"Could not read Wikipedia cache {self.cache_path}: {exc}")
            return {}

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def fetch_many(self, titles: Sequence[str]) -> dict[str, WikipediaMetadata]:
        unique_titles = [title for title in dict.fromkeys(titles) if title.strip()]
        result: dict[str, WikipediaMetadata] = {}
        missing_titles = [title for title in unique_titles if title not in self.cache]

        for title in unique_titles:
            if title in self.cache:
                result[title] = self._metadata_from_cache(title, self.cache[title])

        for start in range(0, len(missing_titles), self.batch_size):
            batch = missing_titles[start : start + self.batch_size]
            try:
                fetched = self._fetch_batch(batch)
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                log(f"Wikipedia lookup failed for {len(batch)} title(s): {exc}")
                fetched = {title: WikipediaMetadata(title=title, missing=True) for title in batch}

            for title, metadata in fetched.items():
                self.cache[title] = {
                    "title": metadata.title,
                    "description": metadata.description,
                    "extract": metadata.extract,
                    "categories": list(metadata.categories),
                    "missing": metadata.missing,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                result[title] = metadata
            self.save_cache()
            time.sleep(self.delay_ms / 1_000)

        return result

    def _metadata_from_cache(self, requested_title: str, payload: dict[str, Any]) -> WikipediaMetadata:
        return WikipediaMetadata(
            title=str(payload.get("title") or requested_title),
            description=str(payload.get("description") or ""),
            extract=str(payload.get("extract") or ""),
            categories=tuple(str(category) for category in payload.get("categories", [])),
            missing=bool(payload.get("missing")),
        )

    def _fetch_batch(self, titles: Sequence[str]) -> dict[str, WikipediaMetadata]:
        props = "description|categories|extracts" if self.description_prop_supported else "categories|extracts"
        data = self._request_query(titles, props)
        if data.get("error") and "description" in props:
            self.description_prop_supported = False
            data = self._request_query(titles, "categories|extracts")

        pages = data.get("query", {}).get("pages", [])
        pages_by_title = {page.get("title", ""): page for page in pages}
        redirect_to: dict[str, str] = {}
        for redirect in data.get("query", {}).get("redirects", []):
            redirect_to[str(redirect.get("from", ""))] = str(redirect.get("to", ""))

        result: dict[str, WikipediaMetadata] = {}
        for requested_title in titles:
            page_title = redirect_to.get(requested_title, requested_title)
            page = pages_by_title.get(page_title) or pages_by_title.get(requested_title)
            if not page or page.get("missing"):
                result[requested_title] = WikipediaMetadata(title=requested_title, missing=True)
                continue

            categories = tuple(
                str(category.get("title", "")).removeprefix("Catégorie:")
                for category in page.get("categories", [])
                if category.get("title")
            )
            result[requested_title] = WikipediaMetadata(
                title=str(page.get("title") or requested_title),
                description=str(page.get("description") or ""),
                extract=str(page.get("extract") or ""),
                categories=categories,
                missing=False,
            )

        return result

    def _request_query(self, titles: Sequence[str], props: str) -> dict[str, Any]:
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
            "prop": props,
            "titles": "|".join(titles),
            "cllimit": "max",
            "exintro": "1",
            "explaintext": "1",
        }
        url = f"{WIKIPEDIA_API_URL}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "project-wikimaster-plant-tagger/1.0 (https://www.wiki-masters.com)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))


def find_collection_search(page: Page) -> Locator | None:
    return get_first_visible(
        [
            page.get_by_placeholder(re.compile("rechercher", re.IGNORECASE)),
            page.locator('input[type="search"]'),
            page.locator('input[placeholder*="Rechercher"], input[placeholder*="rechercher"]'),
        ],
        timeout_ms=1_000,
    )


def select_all_text(locator: Locator) -> None:
    locator.press("ControlOrMeta+A")


def filter_collection(page: Page, query: str, delay_ms: int) -> None:
    search = find_collection_search(page)
    if search is None:
        raise RuntimeError("Could not find the collection search input.")
    search.fill(query)
    page.wait_for_timeout(delay_ms)


def clear_collection_filter(page: Page, delay_ms: int) -> None:
    search = find_collection_search(page)
    if search is None:
        return
    select_all_text(search)
    search.press("Backspace")
    page.wait_for_timeout(delay_ms)


def click_select_mode(page: Page) -> None:
    selector = get_first_visible(
        [
            page.get_by_role("button", name=re.compile("^sélectionner$|^selectionner$", re.IGNORECASE)),
            page.get_by_text(re.compile("^sélectionner$|^selectionner$", re.IGNORECASE)),
        ],
        timeout_ms=2_500,
    )
    if selector is None:
        raise RuntimeError("Could not find the collection Sélectionner button.")
    selector.click(timeout=5_000)
    page.wait_for_timeout(500)


def find_visible_card_click_point(page: Page, card: CardRecord) -> dict[str, float] | None:
    return page.evaluate(
        """
        ({ title, subtitle }) => {
          const normalize = (value) =>
            (value || '')
              .normalize('NFD')
              .replace(/[\\u0300-\\u036f]/g, '')
              .toLowerCase()
              .replace(/\\s+/g, ' ')
              .trim();
          const titleNorm = normalize(title);
          const subtitleNorm = normalize(subtitle);
          const rarityPattern = /^(L|UR|SR|R|PC|C)$/i;
          const candidates = [];

          for (const element of document.querySelectorAll('article, a, button, [role="button"], [role="listitem"], div')) {
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            if (
              style.visibility === 'hidden' ||
              style.display === 'none' ||
              rect.width < 110 ||
              rect.width > 280 ||
              rect.height < 150 ||
              rect.height > 380 ||
              rect.bottom < 0 ||
              rect.top > window.innerHeight
            ) {
              continue;
            }

            const lines = (element.innerText || '').split('\\n').map((line) => normalize(line)).filter(Boolean);
            if (!lines.some((line) => rarityPattern.test(line))) continue;
            const hasTitle = lines.some((line) => line === titleNorm || line.includes(titleNorm) || titleNorm.includes(line));
            if (!hasTitle) continue;
            const hasSubtitle = !subtitleNorm || lines.some((line) => line === subtitleNorm || line.includes(subtitleNorm));
            const score = (hasSubtitle ? 1000 : 0) - rect.top;
            candidates.push({ element, rect, score });
          }

          candidates.sort((a, b) => b.score - a.score);
          const candidate = candidates[0];
          if (!candidate) return null;

          const controls = [...candidate.element.querySelectorAll('input[type="checkbox"], [role="checkbox"]')]
            .map((control) => ({ control, rect: control.getBoundingClientRect() }))
            .filter(({ rect }) => rect.width > 0 && rect.height > 0);
          if (controls.length) {
            const rect = controls[0].rect;
            return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
          }

          return {
            x: candidate.rect.left + candidate.rect.width / 2,
            y: candidate.rect.top + candidate.rect.height / 2,
          };
        }
        """,
        {"title": card.title, "subtitle": card.subtitle},
    )


def select_card(page: Page, card: CardRecord, selection_delay_ms: int) -> None:
    point = find_visible_card_click_point(page, card)
    if point is None:
        filter_collection(page, card.title, selection_delay_ms)
        point = find_visible_card_click_point(page, card)
    if point is None:
        raise RuntimeError(f"Could not find visible card to select: {card.title}")

    page.mouse.click(point["x"], point["y"])
    page.wait_for_timeout(selection_delay_ms)


def dump_visible_controls(page: Page) -> str:
    try:
        controls = page.evaluate(
            """
            () => [...document.querySelectorAll('button, [role="button"], [role="menuitem"], [role="option"], input, select')]
              .map((element) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                if (style.visibility === 'hidden' || style.display === 'none' || rect.width === 0 || rect.height === 0) {
                  return null;
                }
                return [
                  element.tagName.toLowerCase(),
                  element.getAttribute('role') || '',
                  element.getAttribute('aria-label') || '',
                  element.getAttribute('placeholder') || '',
                  (element.innerText || element.value || '').replace(/\\s+/g, ' ').trim(),
                ].filter(Boolean).join(' | ');
              })
              .filter(Boolean)
              .slice(0, 80)
            """
        )
        return "\n".join(f"- {control}" for control in controls)
    except PlaywrightError:
        return "- could not inspect visible controls"


def click_bulk_tag_menu(page: Page) -> None:
    point = page.evaluate(
        """
        () => {
          const normalize = (value) =>
            (value || '')
              .normalize('NFD')
              .replace(/[\\u0300-\\u036f]/g, '')
              .toLowerCase()
              .replace(/\\s+/g, ' ')
              .trim();
          const candidates = [];
          for (const element of document.querySelectorAll('button, [role="button"], a')) {
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            if (style.visibility === 'hidden' || style.display === 'none' || rect.width < 24 || rect.height < 24) {
              continue;
            }
            const text = normalize([
              element.innerText,
              element.getAttribute('aria-label'),
              element.getAttribute('title'),
            ].filter(Boolean).join(' '));
            if (!/(etiquette|etiquettes|tag)/.test(text)) continue;
            if (/toutes les etiquettes|filtre/.test(text)) continue;
            const score =
              (/ajouter|add/.test(text) ? 1000 : 0) +
              (/etiquette|etiquettes/.test(text) ? 200 : 0) -
              rect.top;
            candidates.push({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, score, text });
          }
          candidates.sort((a, b) => b.score - a.score);
          return candidates[0] || null;
        }
        """
    )
    if not point:
        raise RuntimeError("Could not find a bulk étiquette action.\n" + dump_visible_controls(page))
    page.mouse.click(point["x"], point["y"])
    page.wait_for_timeout(600)


def click_tag_option_or_fill(page: Page, target_tag: str) -> None:
    option_pattern = re.compile(rf"^{re.escape(target_tag)}$", re.IGNORECASE)
    option = get_first_visible(
        [
            page.get_by_role("option", name=option_pattern),
            page.get_by_role("menuitem", name=option_pattern),
            page.get_by_role("button", name=option_pattern),
        ],
        timeout_ms=1_000,
    )
    if option is not None:
        option.click(timeout=3_000)
        page.wait_for_timeout(500)
        return

    tag_input = get_first_visible(
        [
            page.get_by_role("combobox", name=re.compile("étiquette|etiquette|tag", re.IGNORECASE)),
            page.get_by_placeholder(re.compile("étiquette|etiquette|tag", re.IGNORECASE)),
            page.locator('input[aria-label*="tiquette"], input[placeholder*="tiquette"]'),
        ],
        timeout_ms=1_000,
    )
    if tag_input is None:
        raise RuntimeError(f"Could not find or enter target tag '{target_tag}'.\n" + dump_visible_controls(page))

    tag_input.fill(target_tag)
    page.wait_for_timeout(300)
    tag_input.press("Enter")
    page.wait_for_timeout(500)


def click_confirmation_if_present(page: Page) -> None:
    confirm = get_first_visible(
        [
            page.get_by_role("button", name=re.compile("appliquer|ajouter|enregistrer|valider|confirmer", re.IGNORECASE)),
            page.get_by_text(re.compile("appliquer|ajouter|enregistrer|valider|confirmer", re.IGNORECASE)),
        ],
        timeout_ms=800,
    )
    if confirm is not None:
        try:
            if confirm.is_enabled(timeout=500):
                confirm.click(timeout=3_000)
                page.wait_for_timeout(700)
        except PlaywrightError:
            pass


def add_tag_to_selected(page: Page, target_tag: str) -> None:
    click_bulk_tag_menu(page)
    click_tag_option_or_fill(page, target_tag)
    click_confirmation_if_present(page)


def visible_card_has_tag(page: Page, card: CardRecord, target_tag: str) -> bool:
    for visible_card in extract_visible_cards(page):
        if normalize_text(visible_card.title) == normalize_text(card.title):
            return has_target_tag(visible_card, target_tag)
    return False


def verify_batch_tags(page: Page, batch: Sequence[CardRecord], target_tag: str, delay_ms: int) -> None:
    missing: list[str] = []
    for card in batch:
        filter_collection(page, card.title, delay_ms)
        found = False
        for _ in range(5):
            if visible_card_has_tag(page, card, target_tag):
                found = True
                break
            page.wait_for_timeout(delay_ms)
        if not found:
            missing.append(card.title)

    clear_collection_filter(page, delay_ms)
    if missing:
        raise RuntimeError(f"Tag verification failed for: {', '.join(missing)}")


def apply_tag_batches(
    page: Page,
    cards: Sequence[CardRecord],
    target_tag: str,
    batch_size: int,
    selection_delay_ms: int,
    batch_delay_ms: int,
) -> list[CardRecord]:
    applied: list[CardRecord] = []
    for start in range(0, len(cards), batch_size):
        batch = list(cards[start : start + batch_size])
        page.goto(COLLECTION_URL, wait_until="domcontentloaded")
        settle_page(page)
        click_select_mode(page)
        clear_collection_filter(page, selection_delay_ms)

        log(f"Selecting {len(batch)} card(s) for batch {start // batch_size + 1}.")
        for card in batch:
            select_card(page, card, selection_delay_ms)

        page.wait_for_timeout(batch_delay_ms)
        add_tag_to_selected(page, target_tag)
        page.wait_for_timeout(batch_delay_ms)
        verify_batch_tags(page, batch, target_tag, selection_delay_ms)
        applied.extend(batch)
        log(f"Applied '{target_tag}' to {len(applied)}/{len(cards)} candidate card(s).")

    return applied


def write_report(
    path: Path,
    cards: Sequence[CardRecord],
    classifications: dict[str, PlantClassification],
    target_tag: str,
    dry_run: bool,
    applied: Sequence[CardRecord] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidates = [card for card in cards if classifications.get(card.key, PlantClassification(False, 0, "")).is_plant_related]
    already_tagged = [card for card in cards if has_target_tag(card, target_tag)]
    applied_keys = {card.key for card in applied}

    lines = [
        "# WikiMasters Plant Tag Report",
        "",
        f"- Timestamp: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Mode: {'dry-run' if dry_run else 'apply'}",
        f"- Target tag: `{target_tag}`",
        f"- Cards scanned: {len(cards)}",
        f"- Candidate cards needing tag: {len(candidates)}",
        f"- Cards already tagged: {len(already_tagged)}",
        f"- Cards applied this run: {len(applied)}",
        "",
    ]

    if not candidates:
        lines.append("No untagged plant taxon cards were found.")
    else:
        lines.extend(
            [
                "| Title | Subtitle | Rarity | Tags | Score | Reason | Action |",
                "|---|---|---:|---|---:|---|---|",
            ]
        )
        for card in candidates:
            classification = classifications[card.key]
            action = "applied" if card.key in applied_keys else ("would apply" if dry_run else "pending")
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(card.title),
                        markdown_cell(card.subtitle),
                        markdown_cell(card.rarity),
                        markdown_cell(", ".join(card.tags)),
                        markdown_cell(classification.score),
                        markdown_cell(classification.reason),
                        markdown_cell(action),
                    ]
                )
                + " |"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with Path(summary_path).open("a", encoding="utf-8") as summary_file:
                summary_file.write("\n".join(lines[:10]) + "\n")
        except OSError as exc:
            log(f"Could not write GitHub step summary: {exc}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tag WikiMasters collection cards for actual plant taxa.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report candidates without changing WikiMasters tags.")
    mode.add_argument("--apply", action="store_true", help="Apply the target tag through the WikiMasters bulk UI.")
    parser.add_argument("--target-tag", default=TARGET_TAG, help="Étiquette to apply. Defaults to 'plante'.")
    parser.add_argument("--max-cards", type=int, default=0, help="Maximum cards to scan. 0 means all loaded cards.")
    parser.add_argument("--batch-size", type=int, default=8, help="Cards to select and tag per mutation batch.")
    parser.add_argument("--scroll-delay-ms", type=int, default=700, help="Delay after each collection scroll.")
    parser.add_argument("--selection-delay-ms", type=int, default=250, help="Delay between selection/search actions.")
    parser.add_argument("--batch-delay-ms", type=int, default=1_500, help="Delay before and after bulk tag application.")
    parser.add_argument("--wikipedia-delay-ms", type=int, default=500, help="Delay between Wikipedia API batches.")
    parser.add_argument("--wikipedia-batch-size", type=int, default=20, help="Wikipedia titles per API request.")
    parser.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH, help="Wikipedia metadata cache path.")
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH, help="Markdown report output path.")
    return parser


def run(args: argparse.Namespace) -> int:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Run `python -m pip install -r requirements.txt` first.")

    dry_run = not args.apply
    email = required_env("WIKIMASTERS_EMAIL")
    password = required_env("WIKIMASTERS_PASSWORD")
    headless = os.environ.get("HEADLESS", "1").lower() not in {"0", "false", "no"}
    target_tag = args.target_tag.strip()
    if not target_tag:
        raise RuntimeError("--target-tag cannot be empty.")
    if args.batch_size < 1:
        raise RuntimeError("--batch-size must be at least 1.")

    page: Page | None = None
    cards: list[CardRecord] = []
    classifications: dict[str, PlantClassification] = {}
    applied: list[CardRecord] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 900},
        )
        context.route("**/*", block_heavy_resources)
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        try:
            page.goto(COLLECTION_URL, wait_until="domcontentloaded")
            login_if_needed(page, email, password)
            page.goto(COLLECTION_URL, wait_until="domcontentloaded")
            settle_page(page)

            cards = scan_collection(page, args.max_cards, args.scroll_delay_ms)
            untagged_cards = [card for card in cards if not has_target_tag(card, target_tag)]
            log(f"Found {len(cards)} scanned card(s); {len(untagged_cards)} do not already have '{target_tag}'.")

            wikipedia = WikipediaClient(args.cache_path, args.wikipedia_delay_ms, args.wikipedia_batch_size)
            metadata_by_title = wikipedia.fetch_many([card.title for card in untagged_cards])
            classifications = {
                card.key: classify_plant_card(card, metadata_by_title.get(card.title))
                for card in untagged_cards
            }
            candidates = [card for card in untagged_cards if classifications[card.key].is_plant_related]
            log(f"Found {len(candidates)} untagged plant taxon candidate card(s).")

            write_report(args.report_path, cards, classifications, target_tag, dry_run=dry_run)
            log(f"Wrote report to {args.report_path}.")

            if dry_run or not candidates:
                if dry_run:
                    log("Dry run completed; no WikiMasters tags were changed.")
                return 0

            applied = apply_tag_batches(
                page,
                candidates,
                target_tag,
                args.batch_size,
                args.selection_delay_ms,
                args.batch_delay_ms,
            )
            write_report(args.report_path, cards, classifications, target_tag, dry_run=False, applied=applied)
            log(f"Run completed successfully. Applied '{target_tag}' to {len(applied)} card(s).")
            return 0
        except Exception as exc:
            log(f"Run failed: {exc}")
            if cards and classifications:
                write_report(args.report_path, cards, classifications, target_tag, dry_run=dry_run, applied=applied)
            save_failure_artifacts(page, "wikimasters-tag-plant-cards-failure")
            return 1
        finally:
            context.close()
            browser.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
