from __future__ import annotations

import unittest

from scripts.sell_shitty_cards import build_arg_parser, parse_active_auction_slots


class SellShittyCardsTests(unittest.TestCase):
    def test_default_start_price_is_six(self) -> None:
        args = build_arg_parser().parse_args([])

        self.assertEqual(args.start_price, 6)

    def test_parse_active_auction_slots_handles_full_french_detail(self) -> None:
        detail = "Enchères actives : 5/5 — annulez une vente ou attendez sa fin pour en lancer une autre."

        self.assertEqual(parse_active_auction_slots(detail), (5, 5))

    def test_parse_active_auction_slots_returns_none_without_counter(self) -> None:
        self.assertIsNone(parse_active_auction_slots("Mettre aux enchères"))


if __name__ == "__main__":
    unittest.main()
