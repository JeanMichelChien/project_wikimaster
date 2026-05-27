#!/usr/bin/env python3
"""Open available WikiMasters packs and reveal every card."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from playwright.sync_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


BASE_URL = "https://www.wiki-masters.com"
PULLS_URL = f"{BASE_URL}/pulls"
ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", "artifacts"))
MAX_PACKS_PER_RUN = int(os.environ.get("MAX_PACKS_PER_RUN", "10"))
MAX_CARD_ADVANCES = int(os.environ.get("MAX_CARD_ADVANCES", "20"))
DEFAULT_TIMEOUT_MS = 12_000


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


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
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeoutError:
        pass


def login_if_needed(page: Page, email: str, password: str) -> None:
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

    submit.click()
    try:
        page.wait_for_url(re.compile(r".*/(pulls|paquets|collection|profile|profil).*"), timeout=DEFAULT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        settle_page(page)

    if "/login" in page.url:
        raise RuntimeError("Login did not complete; still on the login page.")

    log("Login completed.")


def find_open_button(page: Page) -> Locator | None:
    return get_first_visible(
        [
            page.get_by_role("button", name=re.compile("^ouvrir$", re.IGNORECASE)),
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
    try:
        body_text = page.locator("body").inner_text(timeout=2_000)
    except PlaywrightError:
        return None

    match = re.search(r"Carte\s+(\d+)\s*/\s*(\d+)", body_text, re.IGNORECASE)
    if not match:
        return None

    return int(match.group(1)), int(match.group(2))


def click_right_arrow_by_role(page: Page) -> bool:
    locator = get_first_visible(
        [
            page.get_by_role("button", name=re.compile("suivant|next|droite|right|→|›", re.IGNORECASE)),
            page.get_by_label(re.compile("suivant|next|droite|right", re.IGNORECASE)),
        ],
        timeout_ms=750,
    )
    if locator is None:
        return False

    try:
        if locator.is_enabled(timeout=750):
            locator.click(timeout=2_000)
            return True
    except PlaywrightError:
        return False

    return False


def click_right_arrow_by_geometry(page: Page) -> bool:
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
    return click_right_arrow_by_role(page) or click_right_arrow_by_geometry(page)


def reveal_current_pack(page: Page) -> None:
    settle_page(page)

    counter = read_card_counter(page)
    if counter:
        log(f"Pack opened. Card {counter[0]}/{counter[1]}.")
    else:
        log("Pack opened. Card counter not found; using bounded arrow navigation.")

    stagnant_clicks = 0
    previous_counter = counter

    for advance in range(MAX_CARD_ADVANCES):
        counter = read_card_counter(page)
        if counter and counter[0] >= counter[1]:
            log(f"Finished pack at card {counter[0]}/{counter[1]}.")
            return

        clicked = click_right_arrow(page)
        if not clicked:
            log("No clickable right arrow found; assuming pack is finished.")
            return

        page.wait_for_timeout(900)
        current_counter = read_card_counter(page)
        if current_counter:
            log(f"Advanced to card {current_counter[0]}/{current_counter[1]}.")

        if current_counter == previous_counter:
            stagnant_clicks += 1
        else:
            stagnant_clicks = 0

        previous_counter = current_counter
        if stagnant_clicks >= 2:
            log("Card counter stopped changing; stopping this pack.")
            return

    log(f"Reached MAX_CARD_ADVANCES={MAX_CARD_ADVANCES}; stopping this pack.")


def open_all_available_packs(page: Page) -> int:
    opened = 0

    for pack_index in range(1, MAX_PACKS_PER_RUN + 1):
        page.goto(PULLS_URL, wait_until="domcontentloaded")
        settle_page(page)

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
        open_button.click(timeout=5_000)
        page.wait_for_timeout(5_000)

        opened += 1
        reveal_current_pack(page)

    log(f"Reached MAX_PACKS_PER_RUN={MAX_PACKS_PER_RUN}; stopping run.")
    return opened


def main() -> int:
    email = required_env("WIKIMASTERS_EMAIL")
    password = required_env("WIKIMASTERS_PASSWORD")
    headless = os.environ.get("HEADLESS", "1").lower() not in {"0", "false", "no"}

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
            opened = open_all_available_packs(page)
            log(f"Run completed successfully. Packs opened: {opened}.")
            return 0
        except Exception as exc:
            log(f"Run failed: {exc}")
            save_failure_artifacts(page, "wikimasters-open-packs-failure")
            return 1
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
