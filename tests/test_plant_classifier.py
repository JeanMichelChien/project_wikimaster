from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts.env_loader import load_env_file
from scripts.tag_plant_cards import (
    CardRecord,
    WikipediaMetadata,
    classify_plant_card,
    has_tag,
    parse_card_lines,
)


def make_card(title: str, subtitle: str = "", tags: tuple[str, ...] = ()) -> CardRecord:
    return CardRecord(
        key=title.lower().replace(" ", "-"),
        title=title,
        subtitle=subtitle,
        rarity="UR",
        tags=tags,
        visible_text="\n".join(["UR", title, subtitle, *tags]),
    )


class PlantClassifierTests(unittest.TestCase):
    def test_load_env_file_sets_missing_values_without_overriding_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "WIKIMASTERS_TEST_EMAIL=user@example.com",
                        'WIKIMASTERS_TEST_PASSWORD="secret value"',
                    ]
                ),
                encoding="utf-8",
            )
            old_email = os.environ.pop("WIKIMASTERS_TEST_EMAIL", None)
            old_password = os.environ.get("WIKIMASTERS_TEST_PASSWORD")
            os.environ["WIKIMASTERS_TEST_PASSWORD"] = "already-set"

            try:
                load_env_file(env_path)

                self.assertEqual(os.environ["WIKIMASTERS_TEST_EMAIL"], "user@example.com")
                self.assertEqual(os.environ["WIKIMASTERS_TEST_PASSWORD"], "already-set")
            finally:
                os.environ.pop("WIKIMASTERS_TEST_EMAIL", None)
                if old_email is not None:
                    os.environ["WIKIMASTERS_TEST_EMAIL"] = old_email
                if old_password is None:
                    os.environ.pop("WIKIMASTERS_TEST_PASSWORD", None)
                else:
                    os.environ["WIKIMASTERS_TEST_PASSWORD"] = old_password

    def test_visible_taxon_subtitle_is_plant_related(self) -> None:
        card = make_card("Ancolie", "genre de plantes")

        classification = classify_plant_card(card)

        self.assertTrue(classification.is_plant_related)
        self.assertGreaterEqual(classification.score, 3)
        self.assertIn("visible plant taxon term", classification.reason)

    def test_metadata_extract_identifies_plant_species(self) -> None:
        card = make_card("Ardisia ototomoensis")
        metadata = WikipediaMetadata(
            title="Ardisia ototomoensis",
            extract="Ardisia ototomoensis est une espèce de plantes de la famille des Primulaceae.",
            categories=("Plante à fleurs",),
        )

        classification = classify_plant_card(card, metadata)

        self.assertTrue(classification.is_plant_related)
        self.assertIn("Wikipedia", classification.reason)

    def test_generic_botany_topic_is_not_plant_taxon(self) -> None:
        card = make_card("Photosynthèse")
        metadata = WikipediaMetadata(
            title="Photosynthèse",
            description="processus biologique des végétaux",
            categories=("Botanique", "Physiologie végétale"),
        )

        classification = classify_plant_card(card, metadata)

        self.assertFalse(classification.is_plant_related)

    def test_food_or_artwork_fruit_context_is_not_enough(self) -> None:
        card = make_card("La Corbeille de pommes", "peinture de Paul Cézanne")
        metadata = WikipediaMetadata(
            title="La Corbeille de pommes",
            description="tableau de Paul Cézanne",
            categories=("Fruit dans la peinture",),
        )

        classification = classify_plant_card(card, metadata)

        self.assertFalse(classification.is_plant_related)

    def test_person_card_is_not_plant_related(self) -> None:
        card = make_card("Lola Tung", "actrice américaine")
        metadata = WikipediaMetadata(
            title="Lola Tung",
            description="actrice américaine",
            categories=("Actrice américaine", "Naissance en 2002"),
        )

        classification = classify_plant_card(card, metadata)

        self.assertFalse(classification.is_plant_related)

    def test_plant_word_in_film_context_is_not_enough(self) -> None:
        card = make_card("Fleur", "film français")
        metadata = WikipediaMetadata(
            title="Fleur",
            description="film dramatique français",
            categories=("Film français", "Cinéma"),
        )

        classification = classify_plant_card(card, metadata)

        self.assertFalse(classification.is_plant_related)

    def test_single_tree_term_in_extract_is_not_enough(self) -> None:
        card = make_card("Type de médias", "identifiant de format de données sur internet")
        metadata = WikipediaMetadata(
            title="Type de médias",
            description="identifiant de format de données sur internet",
            extract="Un type de médias peut être organisé dans un arbre de correspondances.",
            categories=("Internet", "Format de données"),
        )

        classification = classify_plant_card(card, metadata)

        self.assertFalse(classification.is_plant_related)

    def test_parse_card_lines_extracts_existing_tag(self) -> None:
        card = parse_card_lines(["UR", "Ancolie", "genre de plantes", "plante", "7 201", "4 598"])

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.title, "Ancolie")
        self.assertEqual(card.subtitle, "genre de plantes")
        self.assertTrue(has_tag(card, "plante"))


if __name__ == "__main__":
    unittest.main()
