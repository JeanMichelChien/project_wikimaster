from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts.env_loader import load_env_file
from scripts.tag_collection_cards import (
    CardRecord,
    TagClassification,
    WikipediaMetadata,
    classify_card_for_tag,
    classify_plant_card,
    has_tag,
    iter_page_batches,
    load_candidate_artifact,
    nearby_page_numbers,
    parse_bulk_tag_result_text,
    parse_card_lines,
    parse_tags_arg,
    write_candidate_artifact,
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


class TopicClassifierTests(unittest.TestCase):
    def assert_tag_match(
        self,
        tag: str,
        card: CardRecord,
        metadata: WikipediaMetadata | None = None,
    ) -> None:
        classification = classify_card_for_tag(tag, card, metadata)
        self.assertTrue(classification.is_match, classification.reason)

    def assert_tag_miss(
        self,
        tag: str,
        card: CardRecord,
        metadata: WikipediaMetadata | None = None,
    ) -> None:
        classification = classify_card_for_tag(tag, card, metadata)
        self.assertFalse(classification.is_match, classification.reason)

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

    def test_plant_category_support_does_not_tag_plant_derived_topic(self) -> None:
        card = make_card("Construction en bambou")
        metadata = WikipediaMetadata(
            title="Construction en bambou",
            description="technique de construction utilisant du bambou",
            categories=("Bambou", "Poaceae", "Construction"),
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

    def test_parse_tags_arg_validates_supported_tags(self) -> None:
        self.assertEqual(parse_tags_arg("plante,philo,plante"), ("plante", "philo"))
        with self.assertRaises(ValueError):
            parse_tags_arg("plante,inconnu")

    def test_iter_page_batches_does_not_span_collection_pages(self) -> None:
        cards = [
            make_card("A"),
            make_card("B"),
            make_card("C"),
            make_card("D"),
        ]
        cards = [
            CardRecord(**{**card.__dict__, "page_number": page})
            for card, page in zip(cards, [1, 2, 1, 2], strict=True)
        ]

        batches = list(iter_page_batches(cards, batch_size=1))

        self.assertEqual([(page, [card.title for card in batch]) for page, batch in batches], [(1, ["A"]), (1, ["C"]), (2, ["B"]), (2, ["D"])])

    def test_nearby_page_numbers_prefers_recorded_then_neighbors(self) -> None:
        self.assertEqual(nearby_page_numbers(16, 42, radius=2), (16, 15, 17, 14, 18))

    def test_nearby_page_numbers_respects_collection_bounds(self) -> None:
        self.assertEqual(nearby_page_numbers(1, 2, radius=3), (1, 2))
        self.assertEqual(nearby_page_numbers(None, 42), ())

    def test_parse_bulk_tag_result_single_tagged_card(self) -> None:
        result = parse_bulk_tag_result_text("1 carte étiquetée.")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.tagged, 1)
        self.assertEqual(result.already_tagged, 0)
        self.assertEqual(result.successful_count, 1)

    def test_parse_bulk_tag_result_already_tagged_card(self) -> None:
        result = parse_bulk_tag_result_text("0 carte étiquetée. (1 déjà étiquetée)")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.tagged, 0)
        self.assertEqual(result.already_tagged, 1)
        self.assertEqual(result.successful_count, 1)

    def test_parse_bulk_tag_result_plural_variants(self) -> None:
        result = parse_bulk_tag_result_text("2 cartes étiquetées. (3 cartes déjà étiquetées)")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.tagged, 2)
        self.assertEqual(result.already_tagged, 3)
        self.assertEqual(result.successful_count, 5)

    def test_parse_bulk_tag_result_unrelated_text_is_ambiguous(self) -> None:
        self.assertIsNone(parse_bulk_tag_result_text("Appliquer une étiquette"))

    def test_candidate_artifact_roundtrip_keeps_only_missing_matches(self) -> None:
        candidate = CardRecord(**{**make_card("Ancolie", "genre de plantes").__dict__, "page_number": 1, "page_total": 42})
        already_tagged = CardRecord(**{**make_card("Jacobaea", "genre de plantes", tags=("plante",)).__dict__, "page_number": 2})
        miss = CardRecord(**{**make_card("Photosynthese").__dict__, "page_number": 3})
        classifications = {
            "plante": {
                candidate.key: TagClassification("plante", True, 10, "visible plant taxon term"),
                already_tagged.key: TagClassification("plante", True, 10, "visible plant taxon term"),
                miss.key: TagClassification("plante", False, 0, "no plant taxon evidence found"),
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tag_candidates.json"
            write_candidate_artifact(path, [candidate, already_tagged, miss], classifications, ("plante",))
            loaded = load_candidate_artifact(path)

        self.assertEqual(loaded.tags, ("plante",))
        self.assertEqual([card.title for card in loaded.cards_by_tag["plante"]], ["Ancolie"])
        self.assertEqual(loaded.cards_by_tag["plante"][0].page_total, 42)
        self.assertEqual(loaded.classifications["plante"][candidate.key].score, 10)

    def test_load_candidate_artifact_filters_requested_tags(self) -> None:
        card = make_card("Socrate", "philosophe grec")
        classifications = {
            "plante": {card.key: TagClassification("plante", False, 0, "miss")},
            "philo": {card.key: TagClassification("philo", True, 8, "visible philosophy term")},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tag_candidates.json"
            write_candidate_artifact(path, [card], classifications, ("plante", "philo"))
            loaded = load_candidate_artifact(path, requested_tags=("philo",))

        self.assertEqual(loaded.tags, ("philo",))
        self.assertEqual([card.title for card in loaded.cards_by_tag["philo"]], ["Socrate"])

    def test_philo_matches_philosopher_and_concept(self) -> None:
        philosopher = make_card("Socrate", "philosophe grec")
        philosopher_metadata = WikipediaMetadata(
            title="Socrate",
            description="philosophe grec de l'Antiquite",
            categories=("Philosophe grec antique",),
        )
        concept = make_card("Imperatif categorique", "concept philosophique")
        concept_metadata = WikipediaMetadata(
            title="Imperatif categorique",
            description="concept de philosophie morale d'Emmanuel Kant",
            categories=("Concept de philosophie morale",),
        )

        self.assert_tag_match("philo", philosopher, philosopher_metadata)
        self.assert_tag_match("philo", concept, concept_metadata)

    def test_philo_excludes_unrelated_writer_or_politician(self) -> None:
        writer = make_card("Victor Hugo", "ecrivain francais")
        writer_metadata = WikipediaMetadata(
            title="Victor Hugo",
            description="ecrivain, poete et dramaturge francais",
            extract="Son oeuvre a ete commentee par des philosophes.",
            categories=("Ecrivain francais", "Poete francais"),
        )
        politician = make_card("Pericles", "homme d'Etat, orateur et stratege athenien")
        politician_metadata = WikipediaMetadata(
            title="Pericles",
            description="homme d'Etat athenien",
            categories=("Personnalite politique de la Grece antique",),
        )

        self.assert_tag_miss("philo", writer, writer_metadata)
        self.assert_tag_miss("philo", politician, politician_metadata)

    def test_philo_excludes_generic_ethics_and_category_only_social_topics(self) -> None:
        inquiry = make_card("Leveson Inquiry", "enquete publique sur l'ethique de la presse britannique")
        inquiry_metadata = WikipediaMetadata(
            title="Leveson Inquiry",
            description="enquete publique sur l'ethique de la presse britannique",
            categories=("Presse britannique",),
        )
        friendship = make_card("Amitie homme-femme", "amitie entre un homme et une femme sans lien familial")
        friendship_metadata = WikipediaMetadata(
            title="Amitie homme-femme",
            description="relation sociale",
            categories=("Amitie", "Philosophie"),
        )
        researcher = make_card("Gerard Vergnaud", "mathematicien et chercheur au CNRS francais")
        researcher_metadata = WikipediaMetadata(
            title="Gerard Vergnaud",
            description="mathematicien et chercheur francais",
            categories=("Philosophe francais", "Mathematicien francais"),
        )

        self.assert_tag_miss("philo", inquiry, inquiry_metadata)
        self.assert_tag_miss("philo", friendship, friendship_metadata)
        self.assert_tag_miss("philo", researcher, researcher_metadata)

    def test_scam_matches_fraud_and_ponzi(self) -> None:
        madoff = make_card("Bernard Madoff", "financier americain")
        madoff_metadata = WikipediaMetadata(
            title="Bernard Madoff",
            description="escroc americain condamne pour fraude financiere",
            categories=("Escroc", "Affaire financiere"),
        )
        ponzi = make_card("Chaine de Ponzi", "forme d'escroquerie")

        self.assert_tag_match("scam", madoff, madoff_metadata)
        self.assert_tag_match("scam", ponzi)

    def test_scam_excludes_generic_crime_and_fiction(self) -> None:
        killer = make_card("Jack l'Eventreur", "tueur en serie britannique")
        killer_metadata = WikipediaMetadata(
            title="Jack l'Eventreur",
            description="tueur en serie non identifie",
            categories=("Meurtre non resolu", "Crime"),
        )
        film = make_card("Arnaques, Crimes et Botanique", "film britannique")
        film_metadata = WikipediaMetadata(
            title="Arnaques, Crimes et Botanique",
            description="film policier britannique",
            categories=("Film britannique",),
        )

        self.assert_tag_miss("scam", killer, killer_metadata)
        self.assert_tag_miss("scam", film, film_metadata)

    def test_train_matches_train_objects(self) -> None:
        locomotive = make_card("BB 26000", "locomotive electrique francaise")
        locomotive_metadata = WikipediaMetadata(
            title="BB 26000",
            description="serie de locomotives electriques de la SNCF",
            categories=("Locomotive electrique", "Materiel roulant ferroviaire"),
        )
        tgv = make_card("TGV Duplex", "rame automotrice a grande vitesse")

        self.assert_tag_match("train", locomotive, locomotive_metadata)
        self.assert_tag_match("train", tgv)

    def test_train_excludes_media_stations_lines_and_companies(self) -> None:
        painting = make_card("Le Train dans la neige", "peinture de Claude Monet")
        film = make_card("Le Train (film)", "film franco-italien")
        station = make_card("Gare de Lyon", "gare ferroviaire parisienne")
        line = make_card("Ligne de Paris-Lyon a Marseille-Saint-Charles", "ligne ferroviaire francaise")
        company = make_card("Societe nationale des chemins de fer francais", "entreprise ferroviaire")

        self.assert_tag_miss("train", painting)
        self.assert_tag_miss("train", film)
        self.assert_tag_miss("train", station)
        self.assert_tag_miss("train", line)
        self.assert_tag_miss("train", company)

    def test_train_excludes_operational_railway_topic(self) -> None:
        regularity = make_card("Regularite ferroviaire", "respect par les trains de leurs horaires")
        regularity_metadata = WikipediaMetadata(
            title="Regularite ferroviaire",
            description="respect par les trains de leurs horaires",
            categories=("Exploitation ferroviaire",),
        )

        self.assert_tag_miss("train", regularity, regularity_metadata)

    def test_souterrains_matches_underground_places_and_structures(self) -> None:
        cave = make_card("Grotte Chauvet", "grotte ornee paleolithique")
        cave_metadata = WikipediaMetadata(
            title="Grotte Chauvet",
            description="grotte ornee situee en Ardeche",
            categories=("Grotte ornee",),
        )
        tunnel = make_card("Tunnel sous la Manche", "tunnel ferroviaire sous-marin")
        catacombs = make_card("Catacombes de Paris", "ossuaire municipal souterrain")

        self.assert_tag_match("souterrains", cave, cave_metadata)
        self.assert_tag_match("souterrains", tunnel)
        self.assert_tag_match("souterrains", catacombs)

    def test_souterrains_excludes_events_media_and_metaphors(self) -> None:
        rescue = make_card("Operations de secours de la grotte de Tham Luang", "operation de sauvetage")
        music = make_card("The Velvet Underground", "groupe de rock americain")
        film = make_card("Underground (film, 1995)", "film d'Emir Kusturica")
        district = make_card("Derinkuyu", "district de Turquie")
        district_metadata = WikipediaMetadata(
            title="Derinkuyu",
            description="district de Turquie",
            categories=("District de Turquie", "Ville souterraine"),
        )
        engraving = make_card("Marcus Curtius se precipite dans le gouffre", "gravure de Georges Reverdy")
        writer = make_card("Jacqueline Gruner", "ecrivaine francaise")

        self.assert_tag_miss("souterrains", rescue)
        self.assert_tag_miss("souterrains", music)
        self.assert_tag_miss("souterrains", film)
        self.assert_tag_miss("souterrains", district, district_metadata)
        self.assert_tag_miss("souterrains", engraving)
        self.assert_tag_miss("souterrains", writer)


if __name__ == "__main__":
    unittest.main()
