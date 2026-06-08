#!/usr/bin/env python3
"""Auction WikiMasters cards tagged as low-value resale cards.

The script filters the collection to the `à bicrave` etiquette, opens up to
five matching cards per cycle, and uses the normal WikiMasters auction UI.
It is dry-run by default; pass `--apply` to actually launch auctions. Cards
keep the etiquette after an unsold auction and are retried only after the
visible tagged pool has had one attempt in the persisted retry pass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

try:
    from scripts.tag_collection_cards import (
        ARTIFACT_DIR,
        COLLECTION_URL,
        DEFAULT_TIMEOUT_MS,
        DEFAULT_UI_JITTER_MS,
        BICRAVE_LOW_RARITIES,
        CardRecord,
        PlaywrightError,
        PlaywrightTimeoutError,
        block_heavy_resources,
        click_next_collection_page,
        dump_visible_controls,
        filter_collection,
        find_card_click_point_by_scrolling,
        get_first_visible,
        go_to_collection_page,
        has_target_tag,
        login_if_needed,
        log,
        normalize_text,
        read_collection_page_counter,
        read_card_detail_text,
        required_env,
        reset_collection_scroll,
        save_failure_artifacts,
        scan_collection_page,
        set_ui_jitter_ms,
        settle_page,
        sync_playwright,
        ui_pause,
    )
except ModuleNotFoundError:
    from tag_collection_cards import (  # type: ignore[no-redef]
        ARTIFACT_DIR,
        COLLECTION_URL,
        DEFAULT_TIMEOUT_MS,
        DEFAULT_UI_JITTER_MS,
        BICRAVE_LOW_RARITIES,
        CardRecord,
        PlaywrightError,
        PlaywrightTimeoutError,
        block_heavy_resources,
        click_next_collection_page,
        dump_visible_controls,
        filter_collection,
        find_card_click_point_by_scrolling,
        get_first_visible,
        go_to_collection_page,
        has_target_tag,
        login_if_needed,
        log,
        normalize_text,
        read_collection_page_counter,
        read_card_detail_text,
        required_env,
        reset_collection_scroll,
        save_failure_artifacts,
        scan_collection_page,
        set_ui_jitter_ms,
        settle_page,
        sync_playwright,
        ui_pause,
    )

try:
    from playwright.sync_api import Page
except ImportError:  # Allows py_compile in environments without Playwright.
    Page = object  # type: ignore[assignment]


DEFAULT_TAG = "à bicrave"
DEFAULT_WAIT_SECONDS = 10 * 60 + 10
DEFAULT_MAX_PER_CYCLE = 5
DEFAULT_SCAN_LIMIT = 0
DEFAULT_LOW_RARITY_START_PRICE = 6
DEFAULT_NON_LOW_RARITY_START_PRICE = 40
DEFAULT_SELL_STATE_PATH = ARTIFACT_DIR / "sell_auction_state.json"
NEVER_SELL_RARITIES = {"L"}
ACTIVE_AUCTION_SLOTS_PATTERN = re.compile(r"\bencheres actives\s+(\d+)\s*/\s*(\d+)\b")


class CardUnavailableError(RuntimeError):
    """Raised when a pre-scanned card is no longer visible in the filtered collection."""


class AuctionSlotsFullError(RuntimeError):
    """Raised when WikiMasters reports that no active auction slot is available."""


def normalized_words(value: str) -> list[str]:
    return [word for word in normalize_text(value).split(" ") if word]


def is_sellable_auction_card(card: CardRecord) -> bool:
    """Keep protected rarities out of seller automation even when tagged."""

    return card.rarity.upper() not in NEVER_SELL_RARITIES


def starting_price_for_card(card: CardRecord, low_rarity_price: int, non_low_rarity_price: int) -> int:
    """Use the low-rarity price for normal C/PC resale cards and 40 for manually tagged rarities."""

    if not is_sellable_auction_card(card):
        raise RuntimeError(f"Refusing to auction protected rarity {card.rarity}: {card.title}")
    return low_rarity_price if card.rarity.upper() in BICRAVE_LOW_RARITIES else non_low_rarity_price


def load_attempted_pass_keys(path: Path, target_tag: str) -> set[str]:
    """Load the current auction retry pass so one-shot runs rotate fairly."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as exc:
        log(f"Could not read seller state {path}: {exc}")
        return set()

    tags = data.get("tags", {}) if isinstance(data, dict) else {}
    tag_state = tags.get(target_tag, {}) if isinstance(tags, dict) else {}
    keys = tag_state.get("attempted_pass_keys", []) if isinstance(tag_state, dict) else []
    if not isinstance(keys, list):
        return set()
    return {str(key) for key in keys if str(key)}


def save_attempted_pass_keys(path: Path, target_tag: str, attempted_pass_keys: set[str]) -> None:
    """Persist the current retry pass without touching the cards or their tags."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (json.JSONDecodeError, OSError):
        data = {}

    if not isinstance(data, dict):
        data = {}
    tags = data.setdefault("tags", {})
    if not isinstance(tags, dict):
        tags = {}
        data["tags"] = tags

    tags[target_tag] = {
        "attempted_pass_keys": sorted(attempted_pass_keys),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def choose_next_auction_card(
    candidates: Sequence[CardRecord],
    attempted_pass_keys: set[str],
    attempted_cycle_keys: set[str],
    *,
    restart_pass: bool = True,
) -> tuple[CardRecord | None, bool]:
    """Pick the next tagged card while giving every visible candidate one attempt per pass.

    Unsold cards keep their `à bicrave` tag on WikiMasters, and they become
    eligible again after every other visible tagged card has been attempted once
    in the current persisted retry pass.
    """

    visible_keys = {card.key for card in candidates}
    attempted_pass_keys.intersection_update(visible_keys)

    available_cards = [card for card in candidates if card.key not in attempted_cycle_keys]
    if not available_cards:
        return None, False

    unattempted_cards = [card for card in available_cards if card.key not in attempted_pass_keys]
    if unattempted_cards:
        return unattempted_cards[0], False

    if not restart_pass:
        return None, True

    attempted_pass_keys.clear()
    return available_cards[0], True


def cards_matching_seller_filter(
    scanned: Sequence[CardRecord],
    target_tag: str,
    tag_filter_applied: bool,
) -> list[CardRecord]:
    """Return sellable cards with visible evidence of the target tag."""

    _ = tag_filter_applied
    matching_cards = [card for card in scanned if has_target_tag(card, target_tag)]
    return [card for card in matching_cards if is_sellable_auction_card(card)]


def protected_tagged_cards(scanned: Sequence[CardRecord], target_tag: str) -> list[CardRecord]:
    """Return protected cards only when they visibly carry the target seller tag."""

    return [card for card in scanned if has_target_tag(card, target_tag) and not is_sellable_auction_card(card)]


def detail_text_has_tag(detail_text: str, target_tag: str) -> bool:
    """Return whether an open card detail contains the exact target tag text."""

    normalized_detail = normalize_text(detail_text)
    normalized_target = normalize_text(target_tag)
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(normalized_target)}(?![a-z0-9])", normalized_detail))


def parse_active_auction_slots(detail_text: str) -> tuple[int, int] | None:
    """Return the active-auction count shown on the card detail, when present."""

    match = ACTIVE_AUCTION_SLOTS_PATTERN.search(normalize_text(detail_text))
    if not match:
        return None
    active_count = int(match.group(1))
    active_limit = int(match.group(2))
    return active_count, active_limit


def read_active_auction_slots(page: Page) -> tuple[int, int] | None:
    """Read the current active-auction slot count from the open card detail."""

    return parse_active_auction_slots(read_card_detail_text(page))


def assert_auction_slot_available(page: Page) -> None:
    """Stop before clicking a disabled seller button when WikiMasters slots are full."""

    slots = read_active_auction_slots(page)
    if slots is None:
        return
    active_count, active_limit = slots
    if active_limit > 0 and active_count >= active_limit:
        raise AuctionSlotsFullError(
            f"active auction slots are full ({active_count}/{active_limit}); "
            "waiting for an auction to finish before launching more."
        )


def open_card_detail_has_tag(page: Page, target_tag: str) -> bool:
    """Verify the selected card itself has the tag before opening the auction form."""

    for _ in range(3):
        if detail_text_has_tag(read_card_detail_text(page), target_tag):
            return True
        ui_pause(page, 500)
    return False


def click_control_with_words(page: Page, words: Sequence[str], timeout_ms: int = 3_000) -> bool:
    """Click the best visible button/link whose normalized text contains all words."""

    wanted_words = [normalize_text(word) for word in words if normalize_text(word)]
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        point = page.evaluate(
            """
            (wantedWords) => {
              const normalize = (value) =>
                (value || '')
                  .normalize('NFD')
                  .replace(/[\\u0300-\\u036f]/g, '')
                  .toLowerCase()
                  .replace(/[^a-z0-9'/ -]+/g, ' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
              const controls = [...document.querySelectorAll('button, [role="button"], a, [role="menuitem"], [role="option"]')];
              const candidates = [];
              for (const element of controls) {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                if (
                  style.visibility === 'hidden' ||
                  style.display === 'none' ||
                  style.pointerEvents === 'none' ||
                  element.disabled === true ||
                  element.getAttribute('aria-disabled') === 'true' ||
                  rect.width < 20 ||
                  rect.height < 20 ||
                  rect.bottom < 0 ||
                  rect.top > window.innerHeight
                ) {
                  continue;
                }
                const text = normalize([
                  element.innerText,
                  element.getAttribute('aria-label'),
                  element.getAttribute('title'),
                ].filter(Boolean).join(' '));
                if (!wantedWords.every((word) => text.includes(word))) continue;
                candidates.push({
                  x: rect.left + rect.width / 2,
                  y: rect.top + rect.height / 2,
                  score: (element.tagName.toLowerCase() === 'button' ? 1000 : 0) - rect.top,
                  text,
                });
              }
              candidates.sort((a, b) => b.score - a.score);
              return candidates[0] || null;
            }
            """,
            wanted_words,
        )
        if point:
            page.mouse.click(point["x"], point["y"])
            ui_pause(page, 500)
            return True
        ui_pause(page, 200)
    return False


def auction_form_is_visible(page: Page) -> bool:
    """Return whether the auction form dialog is visible, not just the card detail."""

    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const normalize = (value) =>
                    (value || '')
                      .normalize('NFD')
                      .replace(/[\\u0300-\\u036f]/g, '')
                      .toLowerCase()
                      .replace(/[^a-z0-9'/ -]+/g, ' ')
                      .replace(/\\s+/g, ' ')
                      .trim();
                  const isVisible = (element, rect) => {
                    const style = window.getComputedStyle(element);
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                  };
                  for (const element of document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')) {
                    const rect = element.getBoundingClientRect();
                    if (!isVisible(element, rect) || rect.width < 360 || rect.height < 260) continue;
                    const text = normalize(element.innerText || '');
                    if (
                      text.includes('mettre aux encheres') &&
                      text.includes('mise de depart') &&
                      text.includes('duree') &&
                      text.includes("lancer l'enchere")
                    ) {
                      return true;
                    }
                  }
                  return false;
                }
                """
            )
        )
    except PlaywrightError:
        return False


def wait_for_auction_form(page: Page, timeout_ms: int = 2_500) -> bool:
    """Wait briefly for the auction dialog after clicking the card-detail sell button."""

    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        if auction_form_is_visible(page):
            return True
        ui_pause(page, 150)
    return auction_form_is_visible(page)


def click_auction_form_control_with_words(page: Page, words: Sequence[str], timeout_ms: int = 3_000) -> bool:
    """Click a visible control inside the auction form whose text contains all words."""

    wanted_words = [normalize_text(word) for word in words if normalize_text(word)]
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        point = page.evaluate(
            """
            (wantedWords) => {
              const normalize = (value) =>
                (value || '')
                  .normalize('NFD')
                  .replace(/[\\u0300-\\u036f]/g, '')
                  .toLowerCase()
                  .replace(/[^a-z0-9'/ -]+/g, ' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
              const isVisible = (element, rect) => {
                const style = window.getComputedStyle(element);
                return (
                  style.visibility !== 'hidden' &&
                  style.display !== 'none' &&
                  style.pointerEvents !== 'none' &&
                  rect.width > 0 &&
                  rect.height > 0
                );
              };
              const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')]
                .map((element) => {
                  const rect = element.getBoundingClientRect();
                  if (!isVisible(element, rect) || rect.width < 360 || rect.height < 260) return null;
                  const text = normalize(element.innerText || '');
                  if (
                    !text.includes('mettre aux encheres') ||
                    !text.includes('mise de depart') ||
                    !text.includes('duree') ||
                    !text.includes("lancer l'enchere")
                  ) {
                    return null;
                  }
                  return { element, area: rect.width * rect.height };
                })
                .filter(Boolean)
                .sort((a, b) => a.area - b.area);
              const dialog = dialogs[0]?.element;
              if (!dialog) return null;

              const candidates = [];
              for (const element of dialog.querySelectorAll('button, [role="button"], a, [role="menuitem"], [role="option"]')) {
                const rect = element.getBoundingClientRect();
                if (!isVisible(element, rect)) continue;
                if (element.disabled === true || element.getAttribute('aria-disabled') === 'true') continue;
                const text = normalize([
                  element.innerText,
                  element.getAttribute('aria-label'),
                  element.getAttribute('title'),
                ].filter(Boolean).join(' '));
                if (!wantedWords.every((word) => text.includes(word))) continue;
                candidates.push({
                  x: rect.left + rect.width / 2,
                  y: rect.top + rect.height / 2,
                  score: (element.tagName.toLowerCase() === 'button' ? 1000 : 0) - rect.top,
                });
              }
              candidates.sort((a, b) => b.score - a.score);
              return candidates[0] || null;
            }
            """,
            wanted_words,
        )
        if point:
            page.mouse.click(point["x"], point["y"])
            ui_pause(page, 500)
            return True
        ui_pause(page, 200)
    return False


def collection_tag_filter_looks_active(page: Page, target_tag: str) -> bool:
    """Check the top collection controls for the selected etiquette label."""

    wanted = normalize_text(target_tag)
    try:
        return bool(
            page.evaluate(
                """
                (wanted) => {
                  const normalize = (value) =>
                    (value || '')
                      .normalize('NFD')
                      .replace(/[\\u0300-\\u036f]/g, '')
                      .toLowerCase()
                      .replace(/[^a-z0-9'/ -]+/g, ' ')
                      .replace(/\\s+/g, ' ')
                      .trim();
                  for (const element of document.querySelectorAll('button, [role="button"], [aria-haspopup]')) {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    if (style.visibility === 'hidden' || style.display === 'none') continue;
                    if (rect.top < 120 || rect.top > 360 || rect.width < 80 || rect.height < 24) continue;
                    const text = normalize([
                      element.innerText,
                      element.getAttribute('aria-label'),
                      element.getAttribute('title'),
                    ].filter(Boolean).join(' '));
                    if (text.includes(wanted)) return true;
                  }
                  return false;
                }
                """,
                wanted,
            )
        )
    except PlaywrightError:
        return False


def set_collection_tag_filter(page: Page, target_tag: str, delay_ms: int) -> bool:
    """Use the collection etiquette dropdown so selling does not scan every card."""

    dropdown = get_first_visible(
        [
            page.get_by_role("button", name=re.compile("étiquettes|etiquettes", re.IGNORECASE)),
            page.get_by_text(re.compile("toutes les étiquettes|toutes les etiquettes", re.IGNORECASE)),
        ],
        timeout_ms=2_500,
    )
    if dropdown is not None:
        dropdown.click(timeout=3_000)
    else:
        point = page.evaluate(
            """
            () => {
              const normalize = (value) =>
                (value || '')
                  .normalize('NFD')
                  .replace(/[\\u0300-\\u036f]/g, '')
                  .toLowerCase()
                  .replace(/[^a-z0-9'/ -]+/g, ' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
              const candidates = [];
              for (const element of document.querySelectorAll('button, [role="button"], div')) {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                if (style.visibility === 'hidden' || style.display === 'none' || rect.width < 80 || rect.height < 24) continue;
                const text = normalize([
                  element.innerText,
                  element.getAttribute('aria-label'),
                  element.getAttribute('title'),
                ].filter(Boolean).join(' '));
                if (!/etiquettes/.test(text)) continue;
                if (/selectionner|etiqueter/.test(text)) continue;
                candidates.push({
                  x: rect.left + rect.width / 2,
                  y: rect.top + rect.height / 2,
                  score: (/toutes les etiquettes/.test(text) ? 1000 : 0) - rect.top,
                });
              }
              candidates.sort((a, b) => b.score - a.score);
              return candidates[0] || null;
            }
            """
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
    ui_pause(page, 500)
    wanted = normalize_text(target_tag)
    point = page.evaluate(
        """
        (wanted) => {
          const normalize = (value) =>
            (value || '')
              .normalize('NFD')
              .replace(/[\\u0300-\\u036f]/g, '')
              .toLowerCase()
              .replace(/[^a-z0-9'/ -]+/g, ' ')
              .replace(/\\s+/g, ' ')
              .trim();
          const candidates = [];
          for (const element of document.querySelectorAll('button, [role="button"], [role="option"], [role="menuitem"]')) {
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            if (style.visibility === 'hidden' || style.display === 'none' || rect.width < 20 || rect.height < 20) continue;
            if (rect.top < 120 || rect.top > 430) continue;
            const text = normalize(element.innerText || element.getAttribute('aria-label') || '');
            if (!text.includes(wanted)) continue;
            const role = element.getAttribute('role') || '';
            candidates.push({
              x: rect.left + rect.width / 2,
              y: rect.top + rect.height / 2,
              score:
                (/option|menuitem/.test(role) ? 1000 : 0) +
                (rect.width >= 80 && rect.width <= 320 ? 200 : 0) -
                rect.top,
            });
          }
          candidates.sort((a, b) => b.score - a.score);
          return candidates[0] || null;
        }
        """,
        wanted,
    )
    if not point:
        return False

    page.mouse.click(point["x"], point["y"])
    ui_pause(page, delay_ms)
    return collection_tag_filter_looks_active(page, target_tag)


def scan_seller_filtered_collection(
    page: Page,
    max_cards: int,
    scroll_delay_ms: int,
    empty_page_limit: int = 3,
    no_new_page_limit: int = 5,
) -> list[CardRecord]:
    """Scan a filtered collection and stop after repeated empty filtered pages."""

    records: dict[str, CardRecord] = {}
    empty_pages = 0
    no_new_pages = 0
    inferred_page_number = 1

    while True:
        counter = read_collection_page_counter(page)
        page_number = counter[0] if counter else inferred_page_number
        page_total = counter[1] if counter else None
        page_label = f"{page_number}/{page_total}" if page_total else str(page_number)
        log(f"Scanning filtered collection page {page_label}.")

        remaining = max_cards - len(records) if max_cards > 0 else 0
        if max_cards > 0 and remaining <= 0:
            log(f"Reached --scan-limit={max_cards}; stopping scan.")
            break

        page_cards = [
            replace(card, page_number=page_number, page_total=page_total)
            for card in scan_collection_page(page, remaining, scroll_delay_ms)
        ]
        before_count = len(records)
        for card in page_cards:
            records.setdefault(card.key, card)
        new_count = len(records) - before_count
        log(
            f"Scanned filtered page {page_label}: {len(page_cards)} card(s), "
            f"{new_count} new, {len(records)} total."
        )

        if max_cards > 0 and len(records) >= max_cards:
            log(f"Reached --scan-limit={max_cards}; stopping scan.")
            break

        empty_pages = empty_pages + 1 if not page_cards else 0
        no_new_pages = no_new_pages + 1 if page_cards and new_count == 0 else 0
        if empty_pages >= empty_page_limit:
            log(f"Reached {empty_pages} consecutive empty filtered page(s); stopping scan.")
            break
        if no_new_pages >= no_new_page_limit:
            log(f"Reached {no_new_pages} consecutive filtered page(s) with no new cards; stopping scan.")
            break

        if page_total is not None and page_number >= page_total:
            log(f"Reached final filtered collection page {page_number}/{page_total}.")
            break

        if not click_next_collection_page(page):
            log("No next filtered collection page is available.")
            break
        inferred_page_number = page_number + 1

    return list(records.values())


def collect_tagged_cards(page: Page, target_tag: str, scan_limit: int, scroll_delay_ms: int, delay_ms: int) -> list[CardRecord]:
    """Return currently visible cards carrying the resale tag, using tag filtering first."""

    page.goto(COLLECTION_URL, wait_until="domcontentloaded")
    settle_page(page)
    reset_collection_scroll(page)
    tag_filter_applied = set_collection_tag_filter(page, target_tag, delay_ms)
    if tag_filter_applied:
        log(f"Filtered collection by etiquette '{target_tag}'.")
    else:
        log(f"Could not use etiquette dropdown for '{target_tag}'; falling back to search.")
        filter_collection(page, target_tag, delay_ms)

    scanned = scan_seller_filtered_collection(page, scan_limit, scroll_delay_ms)
    cards = cards_matching_seller_filter(scanned, target_tag, tag_filter_applied)
    protected_cards = protected_tagged_cards(scanned, target_tag)
    if protected_cards:
        log(
            f"Skipping protected rarity card(s) that visibly carry '{target_tag}': "
            + ", ".join(f"{card.title} ({card.rarity})" for card in protected_cards[:20])
            + (f", and {len(protected_cards) - 20} more" if len(protected_cards) > 20 else "")
        )
    higher_rarity = [card for card in cards if card.rarity.upper() not in BICRAVE_LOW_RARITIES]
    if higher_rarity:
        log(
            "Found manually tagged non-C/PC card(s); these will use the higher start price: "
            + ", ".join(f"{card.title} ({card.rarity})" for card in higher_rarity)
        )
    log(f"Found {len(cards)} visible '{target_tag}' card(s) after scanning {len(scanned)} filtered card(s).")
    return cards


def open_collection_card(page: Page, card: CardRecord, target_tag: str, delay_ms: int) -> None:
    """Open one card detail view from the filtered collection."""

    page.goto(COLLECTION_URL, wait_until="domcontentloaded")
    settle_page(page)
    set_collection_tag_filter(page, target_tag, delay_ms)
    go_to_collection_page(page, card.page_number)
    point = find_card_click_point_by_scrolling(page, card, delay_ms)
    if point is None:
        filter_collection(page, card.title, delay_ms)
        point = find_card_click_point_by_scrolling(page, card, delay_ms)
    if point is None:
        raise CardUnavailableError(f"Could not find card to sell: {card.title}")

    page.mouse.click(point["x"], point["y"])
    try:
        page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass
    ui_pause(page, 800)


def fill_starting_bid(page: Page, start_price: int) -> bool:
    """Set the auction starting price to the requested value when the field is visible."""

    return bool(
        page.evaluate(
            """
            (price) => {
              const normalize = (value) =>
                (value || '')
                  .normalize('NFD')
                  .replace(/[\\u0300-\\u036f]/g, '')
                  .toLowerCase()
                  .replace(/[^a-z0-9'/ -]+/g, ' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
              const isVisible = (element, rect) => {
                const style = window.getComputedStyle(element);
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')]
                .map((element) => {
                  const rect = element.getBoundingClientRect();
                  if (!isVisible(element, rect) || rect.width < 360 || rect.height < 260) return null;
                  const text = normalize(element.innerText || '');
                  if (
                    !text.includes('mettre aux encheres') ||
                    !text.includes('mise de depart') ||
                    !text.includes('duree') ||
                    !text.includes("lancer l'enchere")
                  ) {
                    return null;
                  }
                  return { element, area: rect.width * rect.height };
                })
                .filter(Boolean)
                .sort((a, b) => a.area - b.area);
              const dialog = dialogs[0]?.element;
              if (!dialog) return false;

              const inputs = [...dialog.querySelectorAll('input')];
              const candidates = inputs
                .map((input) => {
                  const rect = input.getBoundingClientRect();
                  if (!isVisible(input, rect)) return null;
                  const id = input.id || '';
                  const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                  const container = input.closest('[role="dialog"], form, div');
                  const text = normalize([
                    label?.innerText,
                    input.getAttribute('aria-label'),
                    input.getAttribute('placeholder'),
                    container?.innerText,
                  ].filter(Boolean).join(' '));
                  const score =
                    (/mise de depart|depart|prix|enchere/.test(text) ? 1000 : 0) +
                    (input.type === 'number' ? 100 : 0);
                  return { input, score };
                })
                .filter(Boolean)
                .sort((a, b) => b.score - a.score);
              const candidate = candidates.find((item) => item.score > 0)?.input ||
                (candidates.length === 1 ? candidates[0].input : null);
              if (!candidate) return false;
              candidate.focus();
              candidate.value = String(price);
              candidate.dispatchEvent(new Event('input', { bubbles: true }));
              candidate.dispatchEvent(new Event('change', { bubbles: true }));
              return true;
            }
            """,
            str(start_price),
        )
    )


def set_duration(page: Page, duration_label: str) -> bool:
    """Select the requested auction duration in native or custom controls."""

    changed_select = page.evaluate(
        """
        (wanted) => {
          const normalize = (value) =>
            (value || '')
              .normalize('NFD')
              .replace(/[\\u0300-\\u036f]/g, '')
              .toLowerCase()
              .replace(/[^a-z0-9'/ -]+/g, ' ')
              .replace(/\\s+/g, ' ')
              .trim();
          const isVisible = (element, rect) => {
            const style = window.getComputedStyle(element);
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
          const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')]
            .map((element) => {
              const rect = element.getBoundingClientRect();
              if (!isVisible(element, rect) || rect.width < 360 || rect.height < 260) return null;
              const text = normalize(element.innerText || '');
              if (
                !text.includes('mettre aux encheres') ||
                !text.includes('mise de depart') ||
                !text.includes('duree') ||
                !text.includes("lancer l'enchere")
              ) {
                return null;
              }
              return { element, area: rect.width * rect.height };
            })
            .filter(Boolean)
            .sort((a, b) => a.area - b.area);
          const dialog = dialogs[0]?.element;
          if (!dialog) return false;
          const wantedText = normalize(wanted);
          for (const select of dialog.querySelectorAll('select')) {
            const rect = select.getBoundingClientRect();
            if (!isVisible(select, rect)) continue;
            const options = [...select.options];
            const option = options.find((candidate) => normalize(candidate.textContent || '').includes(wantedText));
            if (!option) continue;
            select.value = option.value;
            select.dispatchEvent(new Event('input', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
          }
          return false;
        }
        """,
        duration_label,
    )
    if changed_select:
        return True

    return click_auction_form_control_with_words(page, normalized_words(duration_label), timeout_ms=2_000)


def close_current_dialog_or_detail(page: Page) -> None:
    """Best-effort close so the next card starts from a predictable state."""

    close_button = get_first_visible(
        [
            page.get_by_role("button", name=re.compile("^terminé$|^termine$|^fermer$|^close$|^ok$", re.IGNORECASE)),
            page.get_by_text(re.compile("^terminé$|^termine$|^fermer$|^close$|^ok$", re.IGNORECASE)),
        ],
        timeout_ms=1_000,
    )
    if close_button is not None:
        try:
            close_button.click(timeout=2_000)
            ui_pause(page, 500)
            return
        except PlaywrightError:
            pass
    try:
        page.keyboard.press("Escape")
        ui_pause(page, 500)
    except PlaywrightError:
        pass


def launch_auction(page: Page, card: CardRecord, start_price: int, duration_label: str, apply: bool) -> bool:
    """Open the auction form and optionally submit it."""

    if apply:
        assert_auction_slot_available(page)

    if not click_control_with_words(page, ("mettre", "encheres"), timeout_ms=3_000):
        if apply:
            assert_auction_slot_available(page)
        log(f"Skipping '{card.title}': no visible 'Mettre aux enchères' button.")
        return False

    if not apply:
        log(f"Dry run: would auction '{card.title}' at {start_price} for {duration_label}.")
        close_current_dialog_or_detail(page)
        return True

    if not wait_for_auction_form(page):
        assert_auction_slot_available(page)
        log(f"Skipping '{card.title}': auction form did not open after clicking 'Mettre aux enchères'.")
        close_current_dialog_or_detail(page)
        return False

    if fill_starting_bid(page, start_price):
        log(f"Set starting bid to {start_price} for '{card.title}'.")
    else:
        log(f"Starting bid field not found for '{card.title}'; leaving WikiMasters default in place.")

    if not set_duration(page, duration_label):
        raise RuntimeError(f"Could not set auction duration '{duration_label}' for {card.title}.\n" + dump_visible_controls(page))
    log(f"Set duration to {duration_label} for '{card.title}'.")

    if not click_auction_form_control_with_words(page, ("lancer", "enchere"), timeout_ms=3_000):
        raise RuntimeError(f"Could not find 'Lancer l'enchère' for {card.title}.\n" + dump_visible_controls(page))

    ui_pause(page, 1_500)
    close_current_dialog_or_detail(page)
    log(f"Launched auction for '{card.title}'.")
    return True


def sleep_between_cycles(seconds: int) -> None:
    """Sleep with periodic terminal updates between 10-minute auction batches."""

    if seconds <= 0:
        return
    remaining = seconds
    log(f"Waiting {seconds} second(s) before the next auction cycle.")
    while remaining > 0:
        step = min(60, remaining)
        time.sleep(step)
        remaining -= step
        if remaining > 0:
            log(f"Still waiting; {remaining} second(s) left.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auction cards tagged with the à bicrave etiquette.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show what would be auctioned without launching auctions.")
    mode.add_argument("--apply", action="store_true", help="Actually launch WikiMasters auctions.")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="Etiquette used to find cards to sell.")
    parser.add_argument("--max-cards-per-cycle", type=int, default=DEFAULT_MAX_PER_CYCLE, help="Maximum auctions launched per cycle.")
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT, help="Maximum tagged cards scanned per cycle. 0 means all.")
    parser.add_argument("--cycles", type=int, default=0, help="Number of cycles to run. 0 means repeat until no new tagged cards remain.")
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT_SECONDS, help="Delay between cycles; defaults to 10 min 10 sec.")
    parser.add_argument(
        "--start-price",
        type=int,
        default=DEFAULT_LOW_RARITY_START_PRICE,
        help="Auction starting price for C/PC cards.",
    )
    parser.add_argument(
        "--non-low-rarity-start-price",
        type=int,
        default=DEFAULT_NON_LOW_RARITY_START_PRICE,
        help="Auction starting price for manually tagged non-C/PC cards.",
    )
    parser.add_argument("--duration", default="10 min", help="Auction duration label to select.")
    parser.add_argument("--scroll-delay-ms", type=int, default=700, help="Delay after each collection scroll.")
    parser.add_argument("--selection-delay-ms", type=int, default=300, help="Delay between card/search UI actions.")
    parser.add_argument(
        "--jitter-ms",
        type=int,
        default=DEFAULT_UI_JITTER_MS,
        help="Maximum tiny random UI pause added after scripted actions. 0 disables jitter.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_SELL_STATE_PATH,
        help="JSON file used to remember which tagged cards were already attempted in the current retry pass.",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Run `python -m pip install -r requirements.txt` first.")
    if args.max_cards_per_cycle < 1 or args.max_cards_per_cycle > 5:
        raise RuntimeError("--max-cards-per-cycle must be between 1 and 5.")
    if args.scan_limit < 0:
        raise RuntimeError("--scan-limit cannot be negative.")
    if args.scan_limit and args.scan_limit < args.max_cards_per_cycle:
        raise RuntimeError("--scan-limit must be 0 or at least --max-cards-per-cycle.")
    if args.cycles < 0:
        raise RuntimeError("--cycles cannot be negative.")
    if args.wait_seconds < 0:
        raise RuntimeError("--wait-seconds cannot be negative.")
    if args.start_price < 1:
        raise RuntimeError("--start-price must be positive.")
    if args.non_low_rarity_start_price < 1:
        raise RuntimeError("--non-low-rarity-start-price must be positive.")
    if args.jitter_ms < 0:
        raise RuntimeError("--jitter-ms cannot be negative.")
    set_ui_jitter_ms(args.jitter_ms)

    dry_run = not args.apply
    email = required_env("WIKIMASTERS_EMAIL")
    password = required_env("WIKIMASTERS_PASSWORD")
    headless = os.environ.get("HEADLESS", "1").lower() not in {"0", "false", "no"}
    attempted_pass_keys = load_attempted_pass_keys(args.state_path, args.tag)
    if attempted_pass_keys:
        log(
            f"Loaded {len(attempted_pass_keys)} previously attempted "
            f"'{args.tag}' card(s) from {args.state_path}."
        )

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

            cycle = 0
            while args.cycles == 0 or cycle < args.cycles:
                cycle += 1
                log(f"Starting auction cycle {cycle}{' (dry run)' if dry_run else ''}.")
                launched = 0
                attempted_this_cycle: set[str] = set()
                candidates = collect_tagged_cards(
                    page,
                    args.tag,
                    args.scan_limit,
                    args.scroll_delay_ms,
                    args.selection_delay_ms,
                )

                while launched < args.max_cards_per_cycle:
                    previous_attempted_pass_keys = set(attempted_pass_keys)
                    card, restarted_pass = choose_next_auction_card(
                        candidates,
                        attempted_pass_keys,
                        attempted_this_cycle,
                    )
                    if restarted_pass:
                        log(
                            f"All visible '{args.tag}' card(s) have had one auction attempt in the current retry pass; "
                            "starting a new retry pass."
                        )
                    if args.apply and attempted_pass_keys != previous_attempted_pass_keys:
                        save_attempted_pass_keys(args.state_path, args.tag, attempted_pass_keys)
                    if card is None:
                        if launched == 0:
                            log(f"No visible '{args.tag}' card(s) available to auction; stopping.")
                            return 0
                        log(f"No more available '{args.tag}' card(s) in this cycle.")
                        break

                    attempted_this_cycle.add(card.key)
                    try:
                        open_collection_card(page, card, args.tag, args.selection_delay_ms)
                    except CardUnavailableError as exc:
                        log(f"Skipping unavailable card: {exc}")
                        continue
                    if not open_card_detail_has_tag(page, args.tag):
                        log(f"Skipping '{card.title}': card detail does not show the '{args.tag}' tag.")
                        close_current_dialog_or_detail(page)
                        continue

                    start_price = starting_price_for_card(card, args.start_price, args.non_low_rarity_start_price)
                    log(f"Preparing auction for '{card.title}' ({card.rarity}) at start price {start_price}.")
                    try:
                        auction_launched = launch_auction(page, card, start_price, args.duration, args.apply)
                    except AuctionSlotsFullError as exc:
                        log(f"Stopping auction cycle early: {exc}")
                        close_current_dialog_or_detail(page)
                        break
                    if auction_launched:
                        launched += 1
                    attempted_pass_keys.add(card.key)
                    if args.apply:
                        save_attempted_pass_keys(args.state_path, args.tag, attempted_pass_keys)

                action = "would launch" if dry_run else "launched"
                log(f"Cycle {cycle} complete: {action} {launched}/{args.max_cards_per_cycle} auction(s).")
                if dry_run:
                    return 0
                if args.cycles and cycle >= args.cycles:
                    return 0
                sleep_between_cycles(args.wait_seconds)

            return 0
        except KeyboardInterrupt:
            log("Interrupted by user.")
            return 130
        except Exception as exc:
            log(f"Run failed: {exc}")
            save_failure_artifacts(page, "wikimasters-sell-shitty-cards-failure", str(exc))
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
