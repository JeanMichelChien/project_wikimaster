from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from scripts.daily_card_summary import (
    OpenedCard,
    aggregate_cards,
    cards_in_window,
    markdown_cell,
    normalize_rarity,
    parse_github_time,
    parse_opened_card_line,
    render_summary_markdown,
)


class DailyCardSummaryTests(unittest.TestCase):
    def test_parse_plain_opened_card_line(self) -> None:
        line = "[2026-06-07T10:00:00+00:00] Opened card: Marie Curie ; rarity=UR"

        card = parse_opened_card_line(line)

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.name, "Marie Curie")
        self.assertEqual(card.rarity, "UR")
        self.assertEqual(card.opened_at, datetime(2026, 6, 7, 10, 0, tzinfo=timezone.utc))

    def test_parse_github_prefixed_card_line_with_emoji_rarity(self) -> None:
        line = (
            "2026-06-07T10:00:01.1234567Z "
            "[2026-06-07T10:00:01+00:00] Opened card: Ada Lovelace ; rarity=\U0001f3c6 UR"
        )

        card = parse_opened_card_line(line)

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.name, "Ada Lovelace")
        self.assertEqual(card.rarity, "UR")
        self.assertEqual(card.opened_at, datetime(2026, 6, 7, 10, 0, 1, 123456, tzinfo=timezone.utc))

    def test_normalize_rarity_handles_unknown_and_emoji_display(self) -> None:
        self.assertEqual(normalize_rarity("\u2754 unknown"), "unknown")
        self.assertEqual(normalize_rarity("\U0001f499 PC"), "PC")
        self.assertEqual(normalize_rarity("not a rarity"), "unknown")

    def test_cards_in_window_filters_to_last_24_hours(self) -> None:
        now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
        cutoff = now - timedelta(hours=24)
        inside = OpenedCard(now - timedelta(hours=23, minutes=59), "Inside", "C")
        boundary = OpenedCard(cutoff, "Boundary", "R")
        outside = OpenedCard(cutoff - timedelta(seconds=1), "Outside", "L")

        cards = cards_in_window([inside, boundary, outside], cutoff, now)

        self.assertEqual([card.name for card in cards], ["Inside", "Boundary"])

    def test_aggregate_cards_counts_duplicates_and_ranks_by_rarity(self) -> None:
        base = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
        cards = [
            OpenedCard(base - timedelta(minutes=1), "Common New", "C", run_id=11, run_url="https://example.test/11"),
            OpenedCard(base - timedelta(hours=3), "Rare Older", "R", run_id=12, run_url="https://example.test/12"),
            OpenedCard(base - timedelta(hours=2), "Ultra Duplicate", "UR", run_id=13, run_url="https://example.test/13"),
            OpenedCard(base - timedelta(hours=1), "Ultra Duplicate", "UR", run_id=14, run_url="https://example.test/14"),
        ]

        summaries = aggregate_cards(cards)

        self.assertEqual([summary.name for summary in summaries], ["Ultra Duplicate", "Rare Older", "Common New"])
        self.assertEqual(summaries[0].count, 2)
        self.assertEqual(summaries[0].latest_opened_at, base - timedelta(hours=1))
        self.assertEqual(sorted(summaries[0].run_urls), [13, 14])

    def test_markdown_cell_escapes_table_special_characters(self) -> None:
        self.assertEqual(markdown_cell("A | B\\C\nD"), "A \\| B\\\\C D")

    def test_render_summary_markdown_includes_empty_state(self) -> None:
        now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
        cutoff = now - timedelta(hours=24)

        markdown = render_summary_markdown([], cutoff=cutoff, now=now, source_run_count=2)

        self.assertIn("No cards were opened in the last 24 hours.", markdown)
        self.assertIn("Open-packs workflow runs checked: `2`", markdown)

    def test_parse_github_time_truncates_long_fractional_seconds(self) -> None:
        parsed = parse_github_time("2026-06-07T10:00:01.1234567Z")

        self.assertEqual(parsed, datetime(2026, 6, 7, 10, 0, 1, 123456, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
