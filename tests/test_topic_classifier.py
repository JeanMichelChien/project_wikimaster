from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.env_loader import load_env_file
from scripts.sell_shitty_cards import (
    cards_matching_seller_filter,
    choose_next_auction_card,
    detail_text_has_tag,
    is_sellable_auction_card,
    load_attempted_pass_keys,
    protected_tagged_cards,
    save_attempted_pass_keys,
    starting_price_for_card,
)
from scripts.tag_collection_cards import (
    CardRecord,
    TagClassification,
    WikipediaMetadata,
    apply_confirmed_tags,
    build_candidates_by_tag,
    build_invalid_existing_by_tag,
    classify_card_for_tag,
    classify_plant_card,
    display_rarity,
    filter_current_tagless_candidates,
    forget_confirmed_tags,
    has_tag,
    iter_page_batches,
    load_candidate_artifact,
    load_confirmed_tag_keys,
    nearby_page_numbers,
    parse_bulk_tag_result_text,
    parse_card_lines,
    parse_selected_count_text,
    parse_tags_arg,
    record_confirmed_tags,
    render_tagged_cards_summary,
    resolve_enabled_tags,
    search_queries_for_card,
    selected_count_is_at_least,
    validate_apply_candidates_for_tag,
    wait_for_selected_count_increase,
    write_candidate_artifact,
)


def make_card(title: str, subtitle: str = "", tags: tuple[str, ...] = (), rarity: str = "UR") -> CardRecord:
    return CardRecord(
        key=title.lower().replace(" ", "-"),
        title=title,
        subtitle=subtitle,
        rarity=rarity,
        tags=tags,
        visible_text="\n".join([rarity, title, subtitle, *tags]),
    )


class FakeSelectionPage:
    def __init__(self, body_texts: list[str]) -> None:
        self.body_texts = body_texts
        self.index = 0
        self.waits: list[int] = []

    def locator(self, selector: str) -> "FakeSelectionPage":
        if selector != "body":
            raise AssertionError(f"Unexpected selector: {selector}")
        return self

    def inner_text(self, timeout: int = 1_000) -> str:
        _ = timeout
        text = self.body_texts[min(self.index, len(self.body_texts) - 1)]
        self.index += 1
        return text

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)


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

    def test_place_with_floral_category_is_not_plant_related(self) -> None:
        card = make_card("Monistrol-sur-Loire", "commune française du département de la Haute-Loire", tags=("plante",))
        metadata = WikipediaMetadata(
            title="Monistrol-sur-Loire",
            description="commune française du département de la Haute-Loire",
            extract="Monistrol-sur-Loire est une commune française.",
            categories=("Villes et villages fleuris", "Plante à fleurs"),
        )

        classification = classify_plant_card(card, metadata)

        self.assertFalse(classification.is_plant_related)
        self.assertIn("hard non-plant context", classification.reason)

    def test_village_tagged_as_plant_is_invalid(self) -> None:
        card = make_card("Dobra Voda", "village croate", tags=("plante",))
        metadata = WikipediaMetadata(
            title="Dobra Voda",
            description="village croate",
            categories=("Village de Croatie", "Villes et villages fleuris"),
        )

        classification = classify_plant_card(card, metadata)

        self.assertFalse(classification.is_plant_related)

    def test_hockey_card_tagged_as_plant_is_invalid(self) -> None:
        card = make_card("Igor Makarov", "joueur de hockey sur glace russe", tags=("plante",))
        metadata = WikipediaMetadata(
            title="Igor Makarov",
            description="joueur de hockey sur glace russe",
            extract="Igor Makarov est un joueur de hockey sur glace.",
            categories=("Joueur russe de hockey sur glace",),
        )

        classification = classify_plant_card(card, metadata)

        self.assertFalse(classification.is_plant_related)

    def test_homonymy_page_with_plant_mention_is_not_plant_related(self) -> None:
        card = make_card("Mayna", "page d'homonymie de Wikimédia")
        metadata = WikipediaMetadata(
            title="Mayna",
            description="page d'homonymie",
            extract="Mayna peut designer un genre de plantes.",
            categories=("Homonymie", "Végétal"),
        )

        classification = classify_plant_card(card, metadata)

        self.assertFalse(classification.is_plant_related)

    def test_invalid_existing_tag_builder_finds_tagged_non_matches(self) -> None:
        plant = make_card("Ancolie", "genre de plantes", tags=("plante",))
        village = make_card("Dobra Voda", "village croate", tags=("plante",))
        untagged = make_card("Socrate", "philosophe grec")
        classifications = {
            "plante": {
                plant.key: TagClassification("plante", True, 10, "visible plant taxon term"),
                village.key: TagClassification("plante", False, -20, "hard non-plant context: village"),
                untagged.key: TagClassification("plante", False, 0, "no plant taxon evidence found"),
            }
        }

        invalid = build_invalid_existing_by_tag([plant, village, untagged], classifications, ("plante",))

        self.assertEqual([card.title for card in invalid["plante"]], ["Dobra Voda"])

    def test_parse_card_lines_extracts_existing_tag(self) -> None:
        card = parse_card_lines(["UR", "Ancolie", "genre de plantes", "plante", "7 201", "4 598"])

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.title, "Ancolie")
        self.assertEqual(card.subtitle, "genre de plantes")
        self.assertTrue(has_tag(card, "plante"))

    def test_parse_card_lines_treats_lone_supported_tag_as_tag_not_subtitle(self) -> None:
        card = parse_card_lines(["C", "Ytteren", "plante", "5 100", "4 900"])

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.title, "Ytteren")
        self.assertEqual(card.subtitle, "")
        self.assertEqual(card.tags, ("plante",))

    def test_parse_tags_arg_validates_supported_tags(self) -> None:
        self.assertEqual(parse_tags_arg("plante,philo,plante"), ("plante", "philo"))
        self.assertEqual(parse_tags_arg("a bicrave"), ("à bicrave",))
        self.assertEqual(parse_tags_arg("riviere"), ("rivière",))
        with self.assertRaises(ValueError):
            parse_tags_arg("plante,inconnu")

    def test_display_rarity_adds_summary_emoji(self) -> None:
        self.assertEqual(display_rarity("L"), "\U0001f451 L")
        self.assertEqual(display_rarity("UR"), "\U0001f3c6 UR")
        self.assertEqual(display_rarity("SR"), "\U0001f497 SR")
        self.assertEqual(display_rarity("R"), "\U0001f49c R")
        self.assertEqual(display_rarity("PC"), "\U0001f535 PC")
        self.assertEqual(display_rarity("C"), "\u26aa C")
        self.assertEqual(display_rarity("not a rarity"), "\u2754 unknown")

    def test_resolve_enabled_tags_defaults_to_all_except_excluded_tag(self) -> None:
        self.assertEqual(
            resolve_enabled_tags(None, None, "à bicrave"),
            ("plante", "philo", "scam", "train", "rivière", "souterrains"),
        )

    def test_resolve_enabled_tags_normalizes_excluded_tag(self) -> None:
        self.assertEqual(
            resolve_enabled_tags(None, None, "a bicrave"),
            ("plante", "philo", "scam", "train", "rivière", "souterrains"),
        )

    def test_resolve_enabled_tags_applies_include_list_then_exclusion(self) -> None:
        self.assertEqual(resolve_enabled_tags("plante,philo,scam", None, "philo"), ("plante", "scam"))

    def test_resolve_enabled_tags_rejects_empty_selection_after_exclusion(self) -> None:
        with self.assertRaisesRegex(ValueError, "No tags remain"):
            resolve_enabled_tags("plante", None, "plante")

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

    def test_iter_page_batches_can_process_later_pages_first(self) -> None:
        cards = [
            CardRecord(**{**make_card("A").__dict__, "page_number": 1}),
            CardRecord(**{**make_card("B").__dict__, "page_number": 3}),
            CardRecord(**{**make_card("C").__dict__, "page_number": 2}),
        ]

        batches = list(iter_page_batches(cards, batch_size=8, descending=True))

        self.assertEqual([(page, [card.title for card in batch]) for page, batch in batches], [(3, ["B"]), (2, ["C"]), (1, ["A"])])

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

    def test_parse_selected_count_text(self) -> None:
        self.assertEqual(parse_selected_count_text("0 carte sélectionnée"), 0)
        self.assertEqual(parse_selected_count_text("2 cartes sélectionnées"), 2)
        self.assertEqual(parse_selected_count_text("0 carte sélectionnée\nÉtiqueter"), 0)
        self.assertIsNone(parse_selected_count_text("Étiqueter"))

    def test_selected_count_is_at_least_waits_for_visible_toolbar_count(self) -> None:
        page = FakeSelectionPage(["0 carte sélectionnée", "1 carte sélectionnée"])

        self.assertEqual(selected_count_is_at_least(page, 1, timeout_ms=50), (True, 1))

    def test_wait_for_selected_count_increase_rejects_transient_selection(self) -> None:
        page = FakeSelectionPage(
            [
                "0 carte sélectionnée",
                "1 carte sélectionnée",
                "0 carte sélectionnée",
            ]
        )

        self.assertIsNone(wait_for_selected_count_increase(page, 0, timeout_ms=5))

    def test_search_queries_for_card_tries_short_normalized_title_variants(self) -> None:
        queries = search_queries_for_card(make_card("Nick Carter, le roi des détectives"))

        self.assertIn("Nick Carter, le roi des détectives", queries)
        self.assertIn("Nick Carter", queries)
        self.assertIn("nick carter le roi des detectives", queries)
        self.assertIn("nick carter le roi", queries)

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

    def test_candidate_artifact_excludes_cards_with_any_existing_tag(self) -> None:
        candidate = make_card("Socrate", "philosophe grec")
        already_tagged = make_card("Diogene", "philosophe grec", tags=("plante",))
        classifications = {
            "philo": {
                candidate.key: TagClassification("philo", True, 8, "visible philosophy term"),
                already_tagged.key: TagClassification("philo", True, 8, "visible philosophy term"),
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tag_candidates.json"
            write_candidate_artifact(path, [candidate, already_tagged], classifications, ("philo",))
            loaded = load_candidate_artifact(path)

        self.assertEqual([card.title for card in loaded.cards_by_tag["philo"]], ["Socrate"])

    def test_candidate_builder_assigns_card_to_first_matching_tag_only(self) -> None:
        card = make_card("Socrate", "philosophe grec")
        classifications = {
            "philo": {card.key: TagClassification("philo", True, 8, "visible philosophy term")},
            "scam": {card.key: TagClassification("scam", True, 8, "test overlap")},
        }

        candidates = build_candidates_by_tag([card], classifications, ("philo", "scam"))

        self.assertEqual([candidate.title for candidate in candidates["philo"]], ["Socrate"])
        self.assertEqual(candidates["scam"], [])

    def test_validate_apply_candidates_rejects_already_tagged_cards(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no existing tags"):
            validate_apply_candidates_for_tag([make_card("Socrate", "philosophe grec", tags=("plante",))], "philo")

    def test_saved_candidates_are_filtered_against_current_tag_state(self) -> None:
        first = make_card("First", "philosophe grec")
        second = make_card("Second", "philosophe grec")
        missing = make_card("Missing", "philosophe grec")
        current_cards = {
            first.title: make_card("First", "philosophe grec", tags=("plante",)),
            second.title: make_card("Second", "philosophe grec"),
            missing.title: None,
        }

        def fake_lookup(page: object, card: CardRecord, delay_ms: int) -> CardRecord | None:
            _ = page, delay_ms
            return current_cards[card.title]

        with patch("scripts.tag_collection_cards.current_card_by_search", side_effect=fake_lookup):
            filtered = filter_current_tagless_candidates(object(), [first, second, missing], "philo", 0)

        self.assertEqual([card.title for card in filtered], ["Second"])

    def test_tagged_cards_summary_groups_non_bicrave_cards_by_tag(self) -> None:
        lines = render_tagged_cards_summary(
            {
                "plante": [make_card("Ancolie", "genre de plantes", rarity="UR")],
                "à bicrave": [make_card("Cheap Town", "commune francaise", rarity="C")],
                "philo": [make_card("Socrate", "philosophe grec", rarity="R")],
            },
            ("plante", "à bicrave", "philo"),
        )
        markdown = "\n".join(lines)

        self.assertIn("## `plante`", markdown)
        self.assertIn("| Ancolie | \U0001f3c6 UR |", markdown)
        self.assertIn("## `philo`", markdown)
        self.assertIn("| Socrate | \U0001f49c R |", markdown)
        self.assertNotIn("Cheap Town", markdown)
        self.assertNotIn("## `à bicrave`", markdown)

    def test_tagged_cards_summary_reports_no_non_bicrave_cards(self) -> None:
        lines = render_tagged_cards_summary(
            {"à bicrave": [make_card("Cheap Town", "commune francaise", rarity="C")]},
            ("à bicrave",),
        )

        self.assertIn("No non-`à bicrave` cards were tagged in this run.", "\n".join(lines))

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

    def test_confirmed_tag_cache_marks_scanned_cards_as_already_tagged(self) -> None:
        plant = make_card("Ancolie", "genre de plantes")
        philosopher = make_card("Socrate", "philosophe grec")

        updated, added_count = apply_confirmed_tags(
            [plant, philosopher],
            {"plante": {plant.key}, "philo": {"missing-key"}},
            ("plante", "philo"),
        )

        self.assertEqual(added_count, 1)
        self.assertTrue(has_tag(updated[0], "plante"))
        self.assertFalse(has_tag(updated[1], "philo"))

    def test_confirmed_tag_cache_roundtrip_and_forget(self) -> None:
        plant = make_card("Ancolie", "genre de plantes")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "confirmed_tags.json"
            record_confirmed_tags("plante", [plant], path)
            self.assertEqual(load_confirmed_tag_keys(path), {"plante": {plant.key}})

            forget_confirmed_tags("plante", [plant], path)
            self.assertEqual(load_confirmed_tag_keys(path), {})

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

    def test_philo_excludes_chronology_pages(self) -> None:
        chronology = make_card(
            "1550 en philosophie",
            "liste d'événements survenus en 1550 dans le domaine de la philosophie",
        )
        chronology_metadata = WikipediaMetadata(
            title="1550 en philosophie",
            description="liste d'événements survenus en 1550 dans le domaine de la philosophie",
            extract="L'année 1550 a été marquée, en philosophie, par les événements suivants.",
            categories=("1550 en philosophie", "Portail:Philosophie/Articles liés"),
        )

        self.assert_tag_miss("philo", chronology, chronology_metadata)

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

    def test_train_excludes_historical_convoys(self) -> None:
        train_de_loos = make_card("Train de Loos", "convoi de déportés")
        train_de_loos_metadata = WikipediaMetadata(
            title="Train de Loos",
            description="convoi de déportés",
            extract="Le Train de Loos est un convoi de déportés affrété en 1944.",
            categories=("Convoi de la déportation des Juifs de France", "Portail:Chemin de fer/Articles liés"),
        )

        self.assert_tag_miss("train", train_de_loos, train_de_loos_metadata)

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

    def test_souterrains_excludes_incidental_category_matches(self) -> None:
        pope = make_card("Jean XIX", "144e pape de l'Eglise catholique, de 1024 a 1032")
        pope_metadata = WikipediaMetadata(
            title="Jean XIX",
            description="144e pape de l'Eglise catholique, de 1024 a 1032",
            categories=("Personnalité inhumée dans les grottes vaticanes",),
        )
        geologist = make_card("Louis de Launay", "geologue, poete, philosophe et economiste francais")
        geologist_metadata = WikipediaMetadata(
            title="Louis de Launay",
            description="geologue, poete, philosophe et economiste francais",
            categories=("Ingenieur du corps des mines",),
        )
        company = make_card("Geovic Mining Corp")
        company_metadata = WikipediaMetadata(
            title="Geovic Mining Corp",
            extract="Geovic Mining Corp est une entreprise minière basée a Denver.",
            categories=("Entreprise minière ayant son siège aux États-Unis", "Portail:Mine/Articles liés"),
        )
        trolleybus = make_card("Trolleybus du tunnel de Tateyama", "ligne de trolleybus de la prefecture de Toyama")
        trolleybus_metadata = WikipediaMetadata(
            title="Trolleybus du tunnel de Tateyama",
            description="ligne de trolleybus de la prefecture de Toyama",
            extract="Le trolleybus du tunnel de Tateyama était une ligne de trolleybus entièrement souterraine.",
            categories=("Trolleybus au Japon",),
        )

        self.assert_tag_miss("souterrains", pope, pope_metadata)
        self.assert_tag_miss("souterrains", geologist, geologist_metadata)
        self.assert_tag_miss("souterrains", company, company_metadata)
        self.assert_tag_miss("souterrains", trolleybus, trolleybus_metadata)

    def test_riviere_matches_rivers_and_watercourses(self) -> None:
        seine = make_card("Seine", "fleuve francais")
        seine_metadata = WikipediaMetadata(
            title="Seine",
            description="fleuve francais",
            categories=("Fleuve cotier en France",),
        )
        allier = make_card("Allier", "")
        allier_metadata = WikipediaMetadata(
            title="Allier",
            description="riviere francaise, principal affluent de la Loire",
            categories=("Cours d'eau en France", "Affluent de la Loire"),
        )
        ruisseau = make_card("Ruisseau du Moulin", "ruisseau francais")
        canal = make_card("Canal du Midi", "canal francais")
        canal_metadata = WikipediaMetadata(
            title="Canal du Midi",
            description="canal francais",
            extract="Le canal est une voie navigable et un cours d'eau artificiel.",
            categories=("Canal en France",),
        )
        wetland = make_card("Garaa Sejnane", "zone humide en Tunisie")
        wetland_metadata = WikipediaMetadata(
            title="Garaa Sejnane",
            description="zone humide en Tunisie",
            extract="La Garaa Sejnane est une zone humide bordant l'oued Sejnane.",
            categories=("Site Ramsar en Tunisie", "Zone humide"),
        )

        self.assert_tag_match("rivière", seine, seine_metadata)
        self.assert_tag_match("rivière", allier, allier_metadata)
        self.assert_tag_match("rivière", ruisseau)
        self.assert_tag_match("rivière", canal, canal_metadata)
        self.assert_tag_match("rivière", wetland, wetland_metadata)

    def test_riviere_excludes_named_places_media_and_other_water_features(self) -> None:
        city = make_card("Riviere-du-Loup", "ville du Quebec")
        city_metadata = WikipediaMetadata(
            title="Riviere-du-Loup",
            description="ville du Quebec",
            categories=("Ville au Quebec",),
        )
        bridge = make_card("Pont de la riviere Kwai", "film britannique")
        lake = make_card("Lac Victoria", "lac africain")
        homonymy = make_card("Riviere Rouge", "page d'homonymie")

        self.assert_tag_miss("rivière", city, city_metadata)
        self.assert_tag_miss("rivière", bridge)
        self.assert_tag_miss("rivière", lake)
        self.assert_tag_miss("rivière", homonymy)

    def test_riviere_excludes_roads_events_and_landforms_near_water(self) -> None:
        street = make_card("Rue de Luc", "rue de Bayonne, en France")
        street_metadata = WikipediaMetadata(
            title="Rue de Luc",
            description="rue de Bayonne, en France",
            extract="La rue de Luc est une voie bayonnaise.",
            categories=("Voie à Bayonne", "Portail:Route/Articles liés"),
        )
        landform = make_card("L'Entonnoir", "cirque naturel de France")
        landform_metadata = WikipediaMetadata(
            title="L'Entonnoir",
            description="cirque naturel de France",
            extract="Il est situé sur le cours supérieur de la rivière Saint-Denis et comporte plusieurs cascades.",
            categories=("Chute d'eau dans le parc national de La Réunion", "Portail:Lacs et cours d'eau/Articles liés"),
        )
        flood = make_card("Inondation du 7 juin 1904 de Mamers")
        flood_metadata = WikipediaMetadata(
            title="Inondation du 7 juin 1904 de Mamers",
            extract="L'inondation est causée par une crue éclair de la Dive, un affluent de l'Orne saosnoise.",
            categories=("Inondation en France", "Portail:Lacs et cours d'eau/Articles liés"),
        )

        self.assert_tag_miss("rivière", street, street_metadata)
        self.assert_tag_miss("rivière", landform, landform_metadata)
        self.assert_tag_miss("rivière", flood, flood_metadata)

    def test_a_bicrave_matches_only_low_rarity_untagged_resale_topics(self) -> None:
        village = make_card("Saint-Cierge-la-Serre", "commune francaise", rarity="C")
        politician = make_card("Jane Doe", "femme politique canadienne", rarity="PC")
        tv_show = make_card("Blue Moon (serie televisee)", "serie televisee quebecoise", rarity="C")
        film = make_card("Le Sud (film, 1983)", "film sorti en 1983", rarity="PC")
        athlete = make_card("Joey Saputo", "joueur de football canadien", rarity="C")
        porn_actor = make_card("Example Star", "actrice pornographique", rarity="PC")

        self.assert_tag_match("à bicrave", village)
        self.assert_tag_match("à bicrave", politician)
        self.assert_tag_match("à bicrave", tv_show)
        self.assert_tag_match("à bicrave", film)
        self.assert_tag_match("à bicrave", athlete)
        self.assert_tag_match("à bicrave", porn_actor)

    def test_a_bicrave_requires_no_other_tag_and_c_or_pc_rarity(self) -> None:
        tagged = make_card("Le Sud (film, 1983)", "film sorti en 1983", tags=("cinema",), rarity="C")
        rare = make_card("Le Sud (film, 1983)", "film sorti en 1983", rarity="R")
        unrelated = make_card("Gare de Lyon", "gare ferroviaire parisienne", rarity="C")

        self.assert_tag_miss("à bicrave", tagged)
        self.assert_tag_miss("à bicrave", rare)
        self.assert_tag_miss("à bicrave", unrelated)

    def test_a_bicrave_apply_guard_rejects_non_low_rarity_candidates(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "only be applied to C/PC"):
            validate_apply_candidates_for_tag(
                [make_card("Not Cheap Enough", "film sorti en 1995", rarity="R")],
                "à bicrave",
            )

        validate_apply_candidates_for_tag(
            [
                make_card("Cheap Town", "commune francaise", rarity="C"),
                make_card("Cheap Film", "film sorti en 1995", rarity="PC"),
            ],
            "à bicrave",
        )

    def test_seller_uses_higher_price_for_manually_tagged_non_low_rarity_cards(self) -> None:
        self.assertEqual(starting_price_for_card(make_card("Cheap Film", rarity="PC"), 10, 40), 10)
        self.assertEqual(starting_price_for_card(make_card("Manual Rare", rarity="R"), 10, 40), 40)
        self.assertEqual(starting_price_for_card(make_card("Manual Ultra Rare", rarity="UR"), 10, 40), 40)

    def test_seller_never_sells_l_rarity_cards_even_when_tagged(self) -> None:
        legendary = make_card("Anna's Archive", tags=("à bicrave",), rarity="L")
        rare = make_card("Manual Rare", tags=("à bicrave",), rarity="R")

        self.assertFalse(is_sellable_auction_card(legendary))
        self.assertTrue(is_sellable_auction_card(rare))
        with self.assertRaisesRegex(RuntimeError, "protected rarity L"):
            starting_price_for_card(legendary, 10, 40)
        self.assertEqual(
            cards_matching_seller_filter([legendary, rare], "à bicrave", tag_filter_applied=True),
            [rare],
        )

    def test_seller_only_reports_protected_cards_when_they_have_target_tag(self) -> None:
        untagged_legendary = make_card("Anna's Archive", rarity="L")
        tagged_legendary = make_card("Protected Tagged", tags=("à bicrave",), rarity="L")

        self.assertEqual(protected_tagged_cards([untagged_legendary, tagged_legendary], "à bicrave"), [tagged_legendary])

    def test_seller_requires_visible_tag_even_when_filter_claims_success(self) -> None:
        missing_chip = make_card("Filtered Card", "commune francaise", rarity="C")
        tagged = make_card("Tagged Card", "film sorti en 1999", tags=("à bicrave",), rarity="PC")

        self.assertEqual(
            cards_matching_seller_filter([missing_chip, tagged], "à bicrave", tag_filter_applied=True),
            [tagged],
        )
        self.assertEqual(
            cards_matching_seller_filter([missing_chip, tagged], "à bicrave", tag_filter_applied=False),
            [tagged],
        )

    def test_seller_detail_text_must_contain_exact_target_tag(self) -> None:
        self.assertTrue(detail_text_has_tag("Etiquettes\nà bicrave\nMettre aux enchères", "à bicrave"))
        self.assertFalse(detail_text_has_tag("Khabib Nurmagomedov\nUR\nMettre aux enchères", "à bicrave"))

    def test_seller_prefers_cards_not_attempted_in_current_visible_pool_pass(self) -> None:
        first = make_card("First", rarity="C")
        second = make_card("Second", rarity="PC")
        attempted_pass_keys = {first.key}

        chosen, restarted = choose_next_auction_card([first, second], attempted_pass_keys, set())

        self.assertEqual(chosen, second)
        self.assertFalse(restarted)
        self.assertEqual(attempted_pass_keys, {first.key})

    def test_seller_restarts_visible_pool_pass_after_all_visible_cards_were_attempted(self) -> None:
        first = make_card("First", rarity="C")
        second = make_card("Second", rarity="PC")
        attempted_pass_keys = {first.key, second.key}

        chosen, restarted = choose_next_auction_card([first, second], attempted_pass_keys, set())

        self.assertEqual(chosen, first)
        self.assertTrue(restarted)
        self.assertEqual(attempted_pass_keys, set())

    def test_seller_does_not_retry_same_card_in_one_cycle(self) -> None:
        first = make_card("First", rarity="C")
        second = make_card("Second", rarity="PC")
        attempted_pass_keys = {first.key, second.key}

        chosen, restarted = choose_next_auction_card([first, second], attempted_pass_keys, {first.key})

        self.assertEqual(chosen, second)
        self.assertTrue(restarted)
        self.assertEqual(attempted_pass_keys, set())

    def test_seller_retry_pass_state_roundtrip_is_per_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sell_state.json"

            save_attempted_pass_keys(path, "à bicrave", {"first", "second"})
            save_attempted_pass_keys(path, "other", {"third"})

            self.assertEqual(load_attempted_pass_keys(path, "à bicrave"), {"first", "second"})
            self.assertEqual(load_attempted_pass_keys(path, "other"), {"third"})
            self.assertEqual(load_attempted_pass_keys(path, "missing"), set())

    def test_a_bicrave_uses_metadata_when_visible_text_is_sparse(self) -> None:
        card = make_card("Tiny Town", "", rarity="C")
        metadata = WikipediaMetadata(
            title="Tiny Town",
            description="commune rurale francaise",
            categories=("Commune en France",),
        )

        self.assert_tag_match("à bicrave", card, metadata)

    def test_a_bicrave_excludes_category_only_false_positives(self) -> None:
        football_rule = make_card("Loi 9 du football", "loi regissant le football", rarity="PC")
        football_rule_metadata = WikipediaMetadata(
            title="Loi 9 du football",
            categories=("Sportif",),
        )
        tunnel = make_card("Tunnel de la Boucle", "tunnel ferroviaire", rarity="PC")
        tunnel_metadata = WikipediaMetadata(
            title="Tunnel de la Boucle",
            extract="Le tunnel se trouve pres d'un bourg.",
            categories=("Bourg",),
        )
        awards = make_card("11e ceremonie des Hong Kong Film Awards", "prix decernes au cinema", rarity="C")
        awards_metadata = WikipediaMetadata(
            title="11e ceremonie des Hong Kong Film Awards",
            extract="La ceremonie recompense des films.",
            categories=("Film",),
        )

        self.assert_tag_miss("à bicrave", football_rule, football_rule_metadata)
        self.assert_tag_miss("à bicrave", tunnel, tunnel_metadata)
        self.assert_tag_miss("à bicrave", awards, awards_metadata)


if __name__ == "__main__":
    unittest.main()
