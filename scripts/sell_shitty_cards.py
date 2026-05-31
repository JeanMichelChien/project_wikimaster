#!/usr/bin/env python3
"""Auction WikiMasters cards tagged as low-value resale cards.

The script filters the collection to the `à bicrave` etiquette, opens up to
five matching cards per cycle, and uses the normal WikiMasters auction UI.
It is dry-run by default; pass `--apply` to actually launch auctions.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Sequence

try:
    from scripts.tag_collection_cards import (
        COLLECTION_URL,
        DEFAULT_TIMEOUT_MS,
        BICRAVE_LOW_RARITIES,
        CardRecord,
        PlaywrightError,
        PlaywrightTimeoutError,
        block_heavy_resources,
        dump_visible_controls,
        filter_collection,
        find_card_click_point_by_scrolling,
        get_first_visible,
        go_to_collection_page,
        has_target_tag,
        login_if_needed,
        log,
        normalize_text,
        required_env,
        reset_collection_scroll,
        save_failure_artifacts,
        scan_collection,
        settle_page,
        sync_playwright,
    )
except ModuleNotFoundError:
    from tag_collection_cards import (  # type: ignore[no-redef]
        COLLECTION_URL,
        DEFAULT_TIMEOUT_MS,
        BICRAVE_LOW_RARITIES,
        CardRecord,
        PlaywrightError,
        PlaywrightTimeoutError,
        block_heavy_resources,
        dump_visible_controls,
        filter_collection,
        find_card_click_point_by_scrolling,
        get_first_visible,
        go_to_collection_page,
        has_target_tag,
        login_if_needed,
        log,
        normalize_text,
        required_env,
        reset_collection_scroll,
        save_failure_artifacts,
        scan_collection,
        settle_page,
        sync_playwright,
    )

try:
    from playwright.sync_api import Page
except ImportError:  # Allows py_compile in environments without Playwright.
    Page = object  # type: ignore[assignment]


DEFAULT_TAG = "à bicrave"
DEFAULT_WAIT_SECONDS = 10 * 60 + 10
DEFAULT_MAX_PER_CYCLE = 5
DEFAULT_SCAN_LIMIT = 50
DEFAULT_LOW_RARITY_START_PRICE = 10
DEFAULT_NON_LOW_RARITY_START_PRICE = 40


class CardUnavailableError(RuntimeError):
    """Raised when a pre-scanned card is no longer visible in the filtered collection."""


def normalized_words(value: str) -> list[str]:
    return [word for word in normalize_text(value).split(" ") if word]


def starting_price_for_card(card: CardRecord, low_rarity_price: int, non_low_rarity_price: int) -> int:
    """Use 10 for normal C/PC resale cards and 40 for manually tagged rarities."""

    return low_rarity_price if card.rarity.upper() in BICRAVE_LOW_RARITIES else non_low_rarity_price


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
            page.wait_for_timeout(500)
            return True
        page.wait_for_timeout(200)
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
    if dropdown is None:
        return False

    dropdown.click(timeout=3_000)
    page.wait_for_timeout(500)
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
          for (const element of document.querySelectorAll('button, [role="button"], [role="option"], [role="menuitem"], div')) {
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            if (style.visibility === 'hidden' || style.display === 'none' || rect.width < 20 || rect.height < 20) continue;
            const text = normalize(element.innerText || element.getAttribute('aria-label') || '');
            if (!text.includes(wanted)) continue;
            candidates.push({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, area: rect.width * rect.height });
          }
          candidates.sort((a, b) => a.area - b.area);
          return candidates[0] || null;
        }
        """,
        wanted,
    )
    if not point:
        return False

    page.mouse.click(point["x"], point["y"])
    page.wait_for_timeout(delay_ms)
    return True


def collect_tagged_cards(page: Page, target_tag: str, scan_limit: int, scroll_delay_ms: int, delay_ms: int) -> list[CardRecord]:
    """Return currently visible cards carrying the resale tag, using tag filtering first."""

    page.goto(COLLECTION_URL, wait_until="domcontentloaded")
    settle_page(page)
    reset_collection_scroll(page)
    if set_collection_tag_filter(page, target_tag, delay_ms):
        log(f"Filtered collection by etiquette '{target_tag}'.")
    else:
        log(f"Could not use etiquette dropdown for '{target_tag}'; falling back to search.")
        filter_collection(page, target_tag, delay_ms)

    scanned = scan_collection(page, scan_limit, scroll_delay_ms)
    cards = [card for card in scanned if has_target_tag(card, target_tag)]
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
    page.wait_for_timeout(800)


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
              const inputs = [...document.querySelectorAll('input')];
              const candidates = inputs
                .map((input) => {
                  const rect = input.getBoundingClientRect();
                  const style = window.getComputedStyle(input);
                  if (style.visibility === 'hidden' || style.display === 'none' || rect.width <= 0 || rect.height <= 0) return null;
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
              const candidate = candidates[0]?.input;
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
          const wantedText = normalize(wanted);
          for (const select of document.querySelectorAll('select')) {
            const rect = select.getBoundingClientRect();
            const style = window.getComputedStyle(select);
            if (style.visibility === 'hidden' || style.display === 'none' || rect.width <= 0 || rect.height <= 0) continue;
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

    duration_control = get_first_visible(
        [
            page.get_by_role("combobox", name=re.compile("durée|duree", re.IGNORECASE)),
            page.get_by_text(re.compile("durée|duree", re.IGNORECASE)),
        ],
        timeout_ms=1_000,
    )
    if duration_control is not None:
        duration_control.click(timeout=3_000)
        page.wait_for_timeout(400)

    return click_control_with_words(page, normalized_words(duration_label), timeout_ms=2_000)


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
            page.wait_for_timeout(500)
            return
        except PlaywrightError:
            pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except PlaywrightError:
        pass


def launch_auction(page: Page, card: CardRecord, start_price: int, duration_label: str, apply: bool) -> bool:
    """Open the auction form and optionally submit it."""

    if not click_control_with_words(page, ("mettre", "encheres"), timeout_ms=3_000):
        log(f"Skipping '{card.title}': no visible 'Mettre aux enchères' button.")
        return False

    if not apply:
        log(f"Dry run: would auction '{card.title}' at {start_price} for {duration_label}.")
        close_current_dialog_or_detail(page)
        return True

    if fill_starting_bid(page, start_price):
        log(f"Set starting bid to {start_price} for '{card.title}'.")
    else:
        log(f"Starting bid field not found for '{card.title}'; leaving WikiMasters default in place.")

    if not set_duration(page, duration_label):
        raise RuntimeError(f"Could not set auction duration '{duration_label}' for {card.title}.\n" + dump_visible_controls(page))
    log(f"Set duration to {duration_label} for '{card.title}'.")

    if not click_control_with_words(page, ("lancer", "enchere"), timeout_ms=3_000):
        raise RuntimeError(f"Could not find 'Lancer l'enchère' for {card.title}.\n" + dump_visible_controls(page))

    page.wait_for_timeout(1_500)
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
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT, help="Maximum tagged cards scanned per cycle.")
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
    return parser


def run(args: argparse.Namespace) -> int:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Run `python -m pip install -r requirements.txt` first.")
    if args.max_cards_per_cycle < 1 or args.max_cards_per_cycle > 5:
        raise RuntimeError("--max-cards-per-cycle must be between 1 and 5.")
    if args.scan_limit < args.max_cards_per_cycle:
        raise RuntimeError("--scan-limit must be at least --max-cards-per-cycle.")
    if args.cycles < 0:
        raise RuntimeError("--cycles cannot be negative.")
    if args.wait_seconds < 0:
        raise RuntimeError("--wait-seconds cannot be negative.")
    if args.start_price < 1:
        raise RuntimeError("--start-price must be positive.")
    if args.non_low_rarity_start_price < 1:
        raise RuntimeError("--non-low-rarity-start-price must be positive.")

    dry_run = not args.apply
    email = required_env("WIKIMASTERS_EMAIL")
    password = required_env("WIKIMASTERS_PASSWORD")
    headless = os.environ.get("HEADLESS", "1").lower() not in {"0", "false", "no"}
    seen_keys: set[str] = set()

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

                while launched < args.max_cards_per_cycle:
                    candidates = collect_tagged_cards(
                        page,
                        args.tag,
                        args.scan_limit,
                        args.scroll_delay_ms,
                        args.selection_delay_ms,
                    )
                    next_cards = [
                        card
                        for card in candidates
                        if card.key not in seen_keys and card.key not in attempted_this_cycle
                    ]
                    if not next_cards:
                        if launched == 0:
                            log(f"No new '{args.tag}' card(s) available to auction; stopping.")
                            return 0
                        log(f"No more available '{args.tag}' card(s) in this cycle.")
                        break

                    card = next_cards[0]
                    attempted_this_cycle.add(card.key)
                    seen_keys.add(card.key)
                    start_price = starting_price_for_card(card, args.start_price, args.non_low_rarity_start_price)
                    log(f"Preparing auction for '{card.title}' ({card.rarity}) at start price {start_price}.")
                    try:
                        open_collection_card(page, card, args.tag, args.selection_delay_ms)
                    except CardUnavailableError as exc:
                        log(f"Skipping unavailable card: {exc}")
                        continue
                    if launch_auction(page, card, start_price, args.duration, args.apply):
                        launched += 1

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
            save_failure_artifacts(page, "wikimasters-sell-shitty-cards-failure")
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
