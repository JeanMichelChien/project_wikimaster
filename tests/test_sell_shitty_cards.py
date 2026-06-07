from __future__ import annotations

import unittest

from scripts.sell_shitty_cards import parse_active_auction_slots


class SellShittyCardsTests(unittest.TestCase):
    def test_parse_active_auction_slots_handles_full_french_detail(self) -> None:
        detail = "Enchères actives : 5/5 — annulez une vente ou attendez sa fin pour en lancer une autre."

        self.assertEqual(parse_active_auction_slots(detail), (5, 5))

    def test_parse_active_auction_slots_returns_none_without_counter(self) -> None:
        self.assertIsNone(parse_active_auction_slots("Mettre aux enchères"))


if __name__ == "__main__":
    unittest.main()
