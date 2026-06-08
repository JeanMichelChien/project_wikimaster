#!/usr/bin/env python3
"""Tag WikiMasters collection cards with low-traffic topic etiquettes.

The script logs into WikiMasters, scans the paginated collection once,
enriches each card with cached French Wikipedia metadata, classifies cards for
the enabled topic tags, and optionally applies missing tags in small UI batches.
Dry-run is the default so tuning classifiers cannot accidentally mutate cards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

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
DEFAULT_CACHE_PATH = ARTIFACT_DIR / "wikimasters_wikipedia_cache.json"
LEGACY_CACHE_PATH = ARTIFACT_DIR / "plant_wikipedia_cache.json"
DEFAULT_REPORT_PATH = ARTIFACT_DIR / "tag_report.md"
DEFAULT_CANDIDATE_PATH = ARTIFACT_DIR / "tag_candidates.json"
DEFAULT_CONFIRMED_TAGS_PATH = ARTIFACT_DIR / "confirmed_tags.json"
DEFAULT_TIMEOUT_MS = 12_000
DEFAULT_UI_JITTER_MS = 80
UI_JITTER_MS = int(os.environ.get("WIKIMASTERS_UI_JITTER_MS", str(DEFAULT_UI_JITTER_MS)))
RARITY_PATTERN = re.compile(r"^(L|UR|SR|R|PC|C)$", re.IGNORECASE)
PAGE_COUNTER_PATTERN = re.compile(r"Page\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
SUPPORTED_TAGS = ("plante", "philo", "scam", "train", "rivière", "souterrains", "à bicrave")
DEFAULT_TAGS = ",".join(SUPPORTED_TAGS)
RARITY_EMOJIS = {
    "L": "\U0001f451",
    "UR": "\U0001f3c6",
    "SR": "\U0001f497",
    "R": "\U0001f49c",
    "PC": "\U0001f535",
    "C": "\u26aa",
    "unknown": "\u2754",
}

# Phrase lists are intentionally conservative: each classifier needs central
# article evidence plus tag-specific exclusions to avoid broad keyword matches.
CATEGORY_IGNORED_PREFIXES = (
    "article ",
    "articles ",
    "bon article",
    "categorie commons",
    "date de ",
    "deces ",
    "infobox ",
    "naissance ",
    "page ",
    "portail ",
    "projet ",
    "wikipedia ",
)
CATEGORY_IGNORED_FRAGMENTS = (
    " articles lies",
    " article lie",
    " avec notice d'autorite",
    " contenant un appel a traduction",
    " contenant un lien mort",
    " utilisant une infobox",
    " a illustrer",
    " ebauche ",
)
COMMON_MEDIA_NEGATIVE_PHRASES = (
    "film",
    "serie televisee",
    "roman",
    "livre",
    "chanson",
    "album",
    "jeu video",
    "peinture",
    "tableau",
    "gravure",
    "bande dessinee",
    "personnage",
)
COMMON_LIST_EVENT_NEGATIVE_PHRASES = (
    "page de liste",
    "liste",
    "chronologie",
    "evenement",
    "ceremonie",
    "prix",
    "festival",
    "accident",
    "catastrophe",
    "bataille",
    "guerre",
)
COMMON_ADMIN_PLACE_NEGATIVE_PHRASES = (
    "commune",
    "ville",
    "village",
    "localite",
    "municipalite",
    "departement",
    "region",
    "province",
    "district",
    "canton",
    "arrondissement",
    "comte",
    "pays",
    "territoire",
)
COMMON_STREET_ROUTE_NEGATIVE_PHRASES = (
    "rue",
    "voie a",
    "voie urbaine",
    "voie publique",
    "route",
    "avenue",
    "boulevard",
    "chemin",
    "impasse",
    "place publique",
)
COMMON_PERSON_NEGATIVE_PHRASES = (
    "acteur",
    "actrice",
    "ecrivain",
    "ecrivaine",
    "homme politique",
    "femme politique",
    "roi",
    "reine",
    "pape",
    "poete",
    "poetesse",
)
COMMON_ORGANIZATION_NEGATIVE_PHRASES = (
    "entreprise",
    "societe",
    "compagnie",
    "corp",
    "corporation",
    "organisation",
)
COMMON_TRANSPORT_LINE_NEGATIVE_PHRASES = (
    "gare",
    "station",
    "ligne ferroviaire",
    "ligne de chemin de fer",
    "ligne de metro",
    "ligne de trolleybus",
    "ligne de bus",
    "voie ferree",
)
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
PLANT_HARD_NEGATIVE_PHRASES = (
    "commune francaise",
    "commune rurale",
    "commune de",
    "commune du",
    "commune des",
    "commune dans",
    "municipalite",
    "village",
    "ville de",
    "ville du",
    "ville americaine",
    "ville britannique",
    "localite",
    "hameau",
    "census-designated place",
    "joueur de hockey",
    "joueuse de hockey",
    "hockey",
    "footballeur",
    "footballeuse",
    "sportif",
    "sportive",
    "club sportif",
    "competition sportive",
    "page d'homonymie",
    "homonymie",
    "page de liste",
)
NEGATIVE_CONTEXT_PHRASES = (
    *COMMON_MEDIA_NEGATIVE_PHRASES,
    *COMMON_PERSON_NEGATIVE_PHRASES,
    *COMMON_ADMIN_PLACE_NEGATIVE_PHRASES,
    *COMMON_ORGANIZATION_NEGATIVE_PHRASES,
    "groupe de rock",
    "barrage",
    "election",
    "football",
    "hockey",
    "joueur de hockey",
    "joueuse de hockey",
    "sportif",
    "sportive",
    "mathematique",
    "maladie",
    "syndrome",
    "champignon",
    "souche",
    "langue",
    "census-designated place",
    "page d'homonymie",
    "homonymie",
)

PHILO_CORE_PHRASES = (
    "philosophe",
    "philosophes",
    "philosophie",
    "philosophique",
    "ecole philosophique",
    "courant philosophique",
    "doctrine philosophique",
    "concept philosophique",
    "argument philosophique",
    "probleme philosophique",
    "oeuvre philosophique",
    "philosophie politique",
    "philosophie morale",
    "philosophie des sciences",
    "metaphysique",
    "epistemologie",
    "ontologie",
    "phenomenologie",
    "existentialisme",
    "stoicisme",
    "platonisme",
    "aristotelisme",
    "utilitarisme",
    "nihilisme",
    "dialectique",
)
PHILO_CATEGORY_PHRASES = (
    "philosophe",
    "philosophes",
    "concept de philosophie",
    "courant philosophique",
    "ecole philosophique",
    "oeuvre philosophique",
    "argument philosophique",
    "institution philosophique",
    "philosophie morale",
    "philosophie politique",
    "philosophie des sciences",
    "metaphysique",
    "epistemologie",
    "ontologie",
)
PHILO_NEGATIVE_PHRASES = (
    *COMMON_MEDIA_NEGATIVE_PHRASES,
    *COMMON_PERSON_NEGATIVE_PHRASES,
    *COMMON_ADMIN_PLACE_NEGATIVE_PHRASES,
    *COMMON_LIST_EVENT_NEGATIVE_PHRASES,
    "chanteur",
    "chanteuse",
    "footballeur",
    "football",
    "groupe de musique",
)
PHILO_NONCENTRAL_BIO_PHRASES = (
    "mathematicien",
    "mathematicienne",
    "chercheur",
    "chercheuse",
    "sociologue",
    "historien",
    "historienne",
    "psychologue",
    "ecrivain",
    "ecrivaine",
)
PHILO_HARD_NEGATIVE_PHRASES = (
    *COMMON_MEDIA_NEGATIVE_PHRASES,
    "chanteur",
    "chanteuse",
    "footballeur",
    "football",
    "groupe de musique",
    "liste d'evenements",
    "chronologie",
)

SCAM_STRONG_PHRASES = (
    "escroquerie",
    "escroqueries",
    "escroc",
    "escrocs",
    "arnaque",
    "arnaques",
    "fraude",
    "fraudes",
    "fraudeur",
    "fraudeurs",
    "frauduleux",
    "frauduleuse",
    "ponzi",
    "pyramide de ponzi",
    "chaine de ponzi",
    "schema de ponzi",
    "systeme pyramidal",
    "vente pyramidale",
    "crime financier",
    "abus de confiance",
    "faux en ecriture",
)
SCAM_CATEGORY_PHRASES = (
    "escroquerie",
    "escroc",
    "arnaque",
    "fraude",
    "fraudeur",
    "ponzi",
    "systeme pyramidal",
    "crime financier",
    "affaire financiere",
)
SCAM_NEGATIVE_PHRASES = (
    *COMMON_MEDIA_NEGATIVE_PHRASES,
    "meurtre",
    "assassinat",
    "guerre",
    "bataille",
    "terrorisme",
    "trafic de drogue",
    "volcan",
)
SCAM_MEDIA_NEGATIVE_PHRASES = (
    *COMMON_MEDIA_NEGATIVE_PHRASES,
)

TRAIN_OBJECT_PHRASES = (
    "train",
    "locomotive",
    "locomotives",
    "rame",
    "rames",
    "rame automotrice",
    "automotrice",
    "automotrices",
    "autorail",
    "autorails",
    "wagon",
    "wagons",
    "voiture voyageurs",
    "materiel roulant",
    "train a grande vitesse",
    "train de voyageurs",
    "train de marchandises",
    "locomotive a vapeur",
    "locomotive electrique",
    "locomotive diesel",
    "tgv",
    "shinkansen",
)
TRAIN_CATEGORY_PHRASES = (
    "locomotive",
    "locomotives",
    "train",
    "materiel roulant",
    "rame automotrice",
    "autorail",
    "wagon",
    "tgv",
    "shinkansen",
)
TRAIN_NEGATIVE_PHRASES = (
    *COMMON_MEDIA_NEGATIVE_PHRASES,
    *COMMON_TRANSPORT_LINE_NEGATIVE_PHRASES,
    *COMMON_ORGANIZATION_NEGATIVE_PHRASES,
    "reseau ferroviaire",
    "compagnie ferroviaire",
    "entreprise ferroviaire",
    "societe ferroviaire",
    "accident ferroviaire",
    "catastrophe ferroviaire",
    "convoi de deportes",
    "deportation",
    "deportes",
    "resistants",
    "occupation allemande",
)

UNDERGROUND_STRUCTURE_PHRASES = (
    "grotte",
    "grottes",
    "caverne",
    "cavernes",
    "gouffre",
    "gouffres",
    "aven",
    "avens",
    "catacombe",
    "catacombes",
    "tunnel",
    "tunnels",
    "mine",
    "mines",
    "bunker",
    "bunkers",
    "abri souterrain",
    "abris souterrains",
    "passage souterrain",
    "passages souterrains",
    "complexe souterrain",
    "reseau souterrain",
    "galerie souterraine",
    "galeries souterraines",
    "ville souterraine",
    "souterrain",
    "souterrains",
    "cavite",
    "cavites",
    "puits de mine",
)
UNDERGROUND_CATEGORY_PHRASES = (
    "grotte",
    "grottes",
    "caverne",
    "catacombes",
    "tunnel",
    "tunnels",
    "mine",
    "mines",
    "bunker",
    "abri souterrain",
    "ouvrage souterrain",
    "souterrain",
    "cavite",
)
UNDERGROUND_NEGATIVE_PHRASES = (
    *COMMON_MEDIA_NEGATIVE_PHRASES,
    *COMMON_ADMIN_PLACE_NEGATIVE_PHRASES,
    *COMMON_PERSON_NEGATIVE_PHRASES,
    *COMMON_ORGANIZATION_NEGATIVE_PHRASES,
    *COMMON_TRANSPORT_LINE_NEGATIVE_PHRASES,
    "groupe de musique",
    "musique underground",
    "culture underground",
    "presse underground",
    "bande dessinee underground",
    "mouvement underground",
    "operation de secours",
    "sauvetage",
    "edit",
    "loi",
    "decret",
    "geologue",
    "speleologue",
    "ingenieur du corps des mines",
    "inhume",
    "inhumee",
    "trolleybus",
)

RIVER_WATERCOURSE_PHRASES = (
    "rivière",
    "fleuve",
    "cours d'eau",
    "affluent",
    "sous-affluent",
    "ruisseau",
    "torrent",
    "oued",
    "wadi",
    "fleuve côtier",
    "rivière endoréique",
    "canal",
    "zone humide",
    "marais",
    "marécage",
    "tourbière",
    "site Ramsar",
)
RIVER_CATEGORY_PHRASES = (
    "rivière",
    "fleuve",
    "cours d'eau",
    "affluent",
    "ruisseau",
    "torrent",
    "oued",
    "canal",
    "zone humide",
    "marais",
    "marécage",
    "tourbière",
    "site Ramsar",
)
RIVER_HARD_NEGATIVE_PHRASES = (
    *COMMON_MEDIA_NEGATIVE_PHRASES,
    *COMMON_LIST_EVENT_NEGATIVE_PHRASES,
    *COMMON_ADMIN_PLACE_NEGATIVE_PHRASES,
    *COMMON_STREET_ROUTE_NEGATIVE_PHRASES,
    *COMMON_PERSON_NEGATIVE_PHRASES,
    *COMMON_TRANSPORT_LINE_NEGATIVE_PHRASES,
    "parc national",
    "pont",
    "barrage",
    "aqueduc",
    "lac",
    "mer",
    "ocean",
    "detroit",
    "golfe",
    "baie",
    "ile",
    "archipel",
    "cascade",
    "chute d'eau",
    "glacier",
    "cirque naturel",
    "cirque geographique",
    "vallee",
    "montagne",
    "colline",
    "bassin versant",
    "bassin hydrographique",
    "delta",
    "estuaire",
    "inondation",
    "crue",
    "page d'homonymie",
    "homonymie",
)
RIVER_CATEGORY_NEGATIVE_PHRASES = (
    *COMMON_ADMIN_PLACE_NEGATIVE_PHRASES,
    *COMMON_STREET_ROUTE_NEGATIVE_PHRASES,
    *COMMON_MEDIA_NEGATIVE_PHRASES,
    *COMMON_LIST_EVENT_NEGATIVE_PHRASES,
    *COMMON_TRANSPORT_LINE_NEGATIVE_PHRASES,
    "pont",
    "barrage",
    "lac",
    "cascade",
    "chute d'eau",
    "cirque",
    "inondation",
    "crue",
    "homonymie",
)

BICRAVE_LOW_RARITIES = {"C", "PC"}
BICRAVE_PLACE_PHRASES = (
    "commune",
    "commune francaise",
    "commune de",
    "ville",
    "village",
    "localite",
    "municipalite",
    "bourg",
)
BICRAVE_POLITICAL_PERSON_PHRASES = (
    "homme politique",
    "femme politique",
    "personnalite politique",
    "politicien",
    "politicienne",
    "depute",
    "deputee",
    "senateur",
    "senatrice",
    "ministre",
    "maire",
)
BICRAVE_MEDIA_PHRASES = (
    "film",
    "court metrage",
    "long metrage",
    "film documentaire",
    "serie televisee",
    "emission de television",
    "emission televisee",
    "programme televise",
    "telefilm",
)
BICRAVE_ATHLETE_PHRASES = (
    "sportif",
    "sportive",
    "athlete",
    "footballeur",
    "footballeuse",
    "joueur de football",
    "joueuse de football",
    "basketteur",
    "basketteuse",
    "tennisman",
    "joueur de tennis",
    "joueuse de tennis",
    "cycliste",
    "nageur",
    "nageuse",
    "skieur",
    "skieuse",
    "boxeur",
    "boxeuse",
    "lutteur",
    "lutteuse",
    "rugbyman",
    "joueur de hockey",
    "joueur de baseball",
    "joueur de cricket",
)
BICRAVE_PORN_PHRASES = (
    "pornographique",
    "acteur pornographique",
    "actrice pornographique",
    "pornographie",
    "star du x",
)
BICRAVE_TOPIC_PHRASES = (
    *BICRAVE_PLACE_PHRASES,
    *BICRAVE_POLITICAL_PERSON_PHRASES,
    *BICRAVE_MEDIA_PHRASES,
    *BICRAVE_ATHLETE_PHRASES,
    *BICRAVE_PORN_PHRASES,
)
BICRAVE_CATEGORY_PHRASES = (
    "commune",
    "ville",
    "village",
    "localite",
    "municipalite",
    "personnalite politique",
    "homme politique",
    "femme politique",
    "depute",
    "senateur",
    "ministre",
    "maire",
    "film",
    "serie televisee",
    "emission de television",
    "telefilm",
    "sportif",
    "sportive",
    "footballeur",
    "footballeuse",
    "basketteur",
    "tennisman",
    "cycliste",
    "acteur pornographique",
    "actrice pornographique",
    "pornographique",
)
BICRAVE_NON_TARGET_PHRASES = (
    "loi",
    "regle",
    "departement",
    "region",
    "province",
    "district",
    "canton",
    "pays",
    "royaume",
    "empire",
    "gare",
    "station",
    "pont",
    "tunnel",
    "ligne ferroviaire",
    "parti politique",
    "organisation politique",
    "election",
    "ceremonie",
    "prix",
    "awards",
    "festival",
    "championnat",
    "competition",
    "bataille",
    "guerre",
    "album",
    "chanson",
    "roman",
    "livre",
    "peinture",
    "tableau",
    "jeu video",
    "club sportif",
    "equipe",
    "stade",
)
BICRAVE_HARD_NEGATIVE_PHRASES = (
    "loi",
    "regle",
    "pont",
    "tunnel",
    "parti politique",
    "organisation politique",
    "election",
    "ceremonie",
    "prix",
    "awards",
    "festival",
    "championnat",
    "competition",
    "club sportif",
    "equipe",
    "stade",
)


@dataclass(frozen=True)
class CardRecord:
    """Normalized data extracted from one visible WikiMasters collection card."""

    key: str
    title: str
    subtitle: str
    rarity: str
    tags: tuple[str, ...]
    visible_text: str
    page_number: int | None = None
    page_total: int | None = None


@dataclass(frozen=True)
class WikipediaMetadata:
    """Small subset of Wikipedia page data used by the tag classifiers."""

    title: str
    description: str = ""
    extract: str = ""
    categories: tuple[str, ...] = ()
    missing: bool = False


@dataclass(frozen=True)
class TagClassification:
    """Classifier result for one card/tag pair."""

    tag: str
    is_match: bool
    score: int
    reason: str

    @property
    def is_plant_related(self) -> bool:
        """Compatibility alias for older plant-only tests/imports."""
        return self.tag == "plante" and self.is_match


@dataclass(frozen=True)
class TagDefinition:
    """Runtime definition for one supported WikiMasters etiquette."""

    tag: str
    description: str
    classifier: Callable[[CardRecord, WikipediaMetadata | None], TagClassification]


@dataclass(frozen=True)
class BulkTagResult:
    """Parsed result from the WikiMasters bulk-tag modal."""

    tagged: int
    already_tagged: int
    raw_text: str

    @property
    def successful_count(self) -> int:
        return self.tagged + self.already_tagged


class SelectionLostError(RuntimeError):
    """Raised when the collection UI drops bulk selection before mutation."""


@dataclass(frozen=True)
class CandidateArtifact:
    """Machine-readable candidates saved after a scan for fast later apply."""

    tags: tuple[str, ...]
    cards_by_tag: dict[str, list[CardRecord]]
    classifications: dict[str, dict[str, TagClassification]]
    cards_scanned: int
    generated_at: str


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def set_ui_jitter_ms(value: int) -> None:
    """Set the tiny extra UI pause used after scripted browser actions."""

    global UI_JITTER_MS
    UI_JITTER_MS = max(0, value)


def ui_pause(page: Page, base_ms: int) -> None:
    """Wait for a base delay plus a short bounded jitter to avoid hammering UI events."""

    jitter_ms = random.randint(0, UI_JITTER_MS) if UI_JITTER_MS > 0 else 0
    page.wait_for_timeout(max(0, base_ms) + jitter_ms)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_text(value: str) -> str:
    """Normalize French UI/API text for stable phrase matching."""

    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    ascii_text = ascii_text.lower().replace("’", "'")
    ascii_text = re.sub(r"[^a-z0-9'/ -]+", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def phrase_matches(text: str, phrases: Sequence[str]) -> list[str]:
    """Return configured phrases found as whole normalized tokens."""

    normalized = normalize_text(text)
    matches: list[str] = []
    for phrase in phrases:
        normalized_phrase = normalize_text(phrase)
        if re.search(rf"(?<![a-z0-9]){re.escape(normalized_phrase)}(?![a-z0-9])", normalized):
            matches.append(phrase)
    return matches


def is_topical_wikipedia_category(category: str) -> bool:
    """Ignore Wikipedia maintenance/navigation categories for topic classifiers."""

    normalized = normalize_text(category).replace(":", " ")
    if not normalized:
        return False
    if any(normalized.startswith(prefix) for prefix in CATEGORY_IGNORED_PREFIXES):
        return False
    return not any(fragment in normalized for fragment in CATEGORY_IGNORED_FRAGMENTS)


def filtered_category_text(metadata: WikipediaMetadata | None) -> str:
    if not metadata or metadata.missing:
        return ""
    return " ".join(category for category in metadata.categories if is_topical_wikipedia_category(category))


def metadata_topic_text(metadata: WikipediaMetadata | None) -> tuple[str, str, str]:
    """Return metadata text for strict topic tags, with non-topical categories removed."""

    if not metadata or metadata.missing:
        return "", "", ""
    return metadata.description, metadata.extract, filtered_category_text(metadata)


def looks_like_latin_taxon(value: str) -> bool:
    """Detect binomial-ish Latin taxon names without catching French titles."""

    normalized = value.strip()
    if not re.fullmatch(r"[A-Z][a-z-]+(?:\s+[a-z-]+){1,2}", normalized):
        return False
    words = normalized.split()
    french_connectors = {"a", "au", "aux", "de", "des", "du", "en", "et", "la", "le", "les", "sous", "sur"}
    return not any(word.lower().strip("-") in french_connectors for word in words[1:])


def normalized_tag(value: str) -> str:
    return normalize_text(value).replace(" ", "-")


def has_tag(card: CardRecord, target_tag: str) -> bool:
    wanted = normalized_tag(target_tag)
    return any(normalized_tag(tag) == wanted for tag in card.tags)


def has_any_tag(card: CardRecord) -> bool:
    return bool(card.tags)


def has_target_tag(card: CardRecord, target_tag: str) -> bool:
    wanted = normalized_tag(target_tag)
    visible_lines = [normalized_tag(line) for line in card.visible_text.splitlines()]
    return has_tag(card, target_tag) or wanted in visible_lines


def validate_apply_candidates_for_tag(cards: Sequence[CardRecord], target_tag: str) -> None:
    """Abort before UI mutation if saved candidates violate hard tag invariants."""

    already_tagged = [card for card in cards if has_any_tag(card)]
    if already_tagged:
        details = ", ".join(f"{card.title} ({', '.join(card.tags)})" for card in already_tagged[:10])
        raise RuntimeError(
            "Tags can only be applied to cards with no existing tags. "
            f"Refusing {len(already_tagged)} already-tagged candidate(s): {details}"
        )

    if normalized_tag(target_tag) != normalized_tag("à bicrave"):
        return

    wrong_rarity = [card for card in cards if card.rarity.upper() not in BICRAVE_LOW_RARITIES]
    if wrong_rarity:
        details = ", ".join(f"{card.title} ({card.rarity})" for card in wrong_rarity[:10])
        raise RuntimeError(
            "`à bicrave` can only be applied to C/PC cards. "
            f"Refusing {len(wrong_rarity)} non-C/PC candidate(s): {details}"
        )


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
    """Convert raw visible card text lines into a structured card record."""

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
        if (
            not subtitle
            and looks_like_tag_line(line)
            and normalized_tag(line) in {normalized_tag(tag) for tag in SUPPORTED_TAGS}
        ):
            tags.append(line)
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


def visible_topic_text(card: CardRecord) -> str:
    return f"{card.title} {card.subtitle} {' '.join(card.tags)}"


def metadata_text(metadata: WikipediaMetadata | None) -> tuple[str, str, str]:
    if not metadata or metadata.missing:
        return "", "", ""
    return metadata.description, metadata.extract, " ".join(metadata.categories)


def unique_reason(reasons: Sequence[str], fallback: str) -> str:
    return "; ".join(dict.fromkeys(reasons)) or fallback


def classify_plante_card(card: CardRecord, metadata: WikipediaMetadata | None = None) -> TagClassification:
    """Match actual plant taxa, not broad botany or plant-derived topics."""

    visible_text = f"{card.title} {card.subtitle} {' '.join(card.tags)}"
    category_text = ""
    description_text = ""
    extract_text = ""
    if metadata and not metadata.missing:
        category_text = filtered_category_text(metadata)
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
    hard_negative = phrase_matches(" ".join([card.subtitle, description_text]), PLANT_HARD_NEGATIVE_PHRASES)

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
    if hard_negative:
        score -= 20
        reasons.append(f"hard non-plant context: {hard_negative[0]}")

    has_taxon_evidence = bool(
        visible_taxon
        or description_taxon
        or extract_taxon
        or (category_taxon and looks_like_latin_taxon(card.title))
    )
    is_match = has_taxon_evidence and score >= 4 and not hard_negative
    reason = unique_reason(reasons, "no plant taxon evidence found")
    return TagClassification(tag="plante", is_match=is_match, score=score, reason=reason)


def classify_plant_card(card: CardRecord, metadata: WikipediaMetadata | None = None) -> TagClassification:
    return classify_plante_card(card, metadata)


def classify_philo_card(card: CardRecord, metadata: WikipediaMetadata | None = None) -> TagClassification:
    """Match core philosophy people, concepts, schools, works, and institutions."""

    visible_text = visible_topic_text(card)
    description_text, extract_text, category_text = metadata_topic_text(metadata)
    metadata_primary_text = " ".join([description_text, category_text])
    score = 0
    reasons: list[str] = []

    visible_core = phrase_matches(visible_text, PHILO_CORE_PHRASES)
    description_core = phrase_matches(description_text, PHILO_CORE_PHRASES)
    category_core = phrase_matches(category_text, PHILO_CATEGORY_PHRASES)
    extract_core = phrase_matches(extract_text, PHILO_CORE_PHRASES)
    negative = phrase_matches(" ".join([visible_text, metadata_primary_text]), PHILO_NEGATIVE_PHRASES)
    hard_negative = phrase_matches(" ".join([visible_text, metadata_primary_text]), PHILO_HARD_NEGATIVE_PHRASES)
    noncentral_bio = phrase_matches(" ".join([visible_text, description_text]), PHILO_NONCENTRAL_BIO_PHRASES)

    if visible_core:
        score += 9
        reasons.append(f"visible philosophy term: {visible_core[0]}")
    if description_core:
        score += 8
        reasons.append(f"Wikipedia description philosophy term: {description_core[0]}")
    if category_core:
        score += 7
        reasons.append(f"Wikipedia category philosophy term: {category_core[0]}")
    if extract_core:
        score += 2
        reasons.append(f"Wikipedia extract philosophy term: {extract_core[0]}")
    if negative:
        score -= 5
        reasons.append(f"non-core philosophy context: {negative[0]}")
    if noncentral_bio and not (visible_core or description_core):
        score -= 6
        reasons.append(f"category-only philosophy on non-central biography: {noncentral_bio[0]}")

    chronology_page = bool(
        re.fullmatch(r"\d{3,4} en philosophie", normalize_text(card.title))
        or phrase_matches(" ".join([visible_text, description_text]), ("liste d'événements",))
    )
    if chronology_page:
        score -= 8
        reasons.append("chronology page rather than core philosophy topic")

    primary_evidence = bool(visible_core or description_core or (category_core and extract_core))
    is_match = primary_evidence and score >= 6 and not hard_negative and not chronology_page
    return TagClassification(
        tag="philo",
        is_match=is_match,
        score=score,
        reason=unique_reason(reasons, "no core philosophy evidence found"),
    )


def classify_scam_card(card: CardRecord, metadata: WikipediaMetadata | None = None) -> TagClassification:
    """Match articles where fraud, scams, or Ponzi-style schemes are central."""

    visible_text = visible_topic_text(card)
    description_text, extract_text, category_text = metadata_topic_text(metadata)
    primary_text = " ".join([visible_text, description_text, category_text])
    score = 0
    reasons: list[str] = []

    visible_strong = phrase_matches(visible_text, SCAM_STRONG_PHRASES)
    description_strong = phrase_matches(description_text, SCAM_STRONG_PHRASES)
    category_strong = list(
        dict.fromkeys(phrase_matches(category_text, SCAM_CATEGORY_PHRASES) + phrase_matches(category_text, SCAM_STRONG_PHRASES))
    )
    extract_strong = phrase_matches(extract_text, SCAM_STRONG_PHRASES)
    negative = phrase_matches(primary_text, SCAM_NEGATIVE_PHRASES)
    media_negative = phrase_matches(primary_text, SCAM_MEDIA_NEGATIVE_PHRASES)

    if visible_strong:
        score += 9
        reasons.append(f"visible central fraud term: {visible_strong[0]}")
    if description_strong:
        score += 8
        reasons.append(f"Wikipedia description central fraud term: {description_strong[0]}")
    if category_strong:
        score += 7
        reasons.append(f"Wikipedia category central fraud term: {category_strong[0]}")
    if extract_strong:
        score += 2
        reasons.append(f"Wikipedia extract fraud support: {extract_strong[0]}")
    if negative:
        score -= 7
        reasons.append(f"non-scam context: {negative[0]}")

    primary_evidence = bool(visible_strong or description_strong or category_strong)
    is_match = primary_evidence and score >= 6 and not media_negative
    return TagClassification(
        tag="scam",
        is_match=is_match,
        score=score,
        reason=unique_reason(reasons, "no central fraud/scam evidence found"),
    )


def classify_train_card(card: CardRecord, metadata: WikipediaMetadata | None = None) -> TagClassification:
    """Match train objects only, excluding stations, media, companies, and events."""

    visible_text = visible_topic_text(card)
    description_text, extract_text, category_text = metadata_topic_text(metadata)
    primary_text = " ".join([visible_text, description_text, category_text])
    score = 0
    reasons: list[str] = []

    visible_object = phrase_matches(visible_text, TRAIN_OBJECT_PHRASES)
    description_object = phrase_matches(description_text, TRAIN_OBJECT_PHRASES)
    category_object = list(
        dict.fromkeys(phrase_matches(category_text, TRAIN_CATEGORY_PHRASES) + phrase_matches(category_text, TRAIN_OBJECT_PHRASES))
    )
    extract_object = phrase_matches(extract_text, TRAIN_OBJECT_PHRASES)
    negative = phrase_matches(primary_text, TRAIN_NEGATIVE_PHRASES)

    if visible_object:
        score += 10
        reasons.append(f"visible train object term: {visible_object[0]}")
    if description_object:
        score += 8
        reasons.append(f"Wikipedia description train object term: {description_object[0]}")
    if category_object:
        score += 7
        reasons.append(f"Wikipedia category train object term: {category_object[0]}")
    if extract_object:
        score += 2
        reasons.append(f"Wikipedia extract train object support: {extract_object[0]}")
    if negative:
        score -= 10
        reasons.append(f"excluded non-object train context: {negative[0]}")

    primary_evidence = bool(visible_object or description_object or category_object)
    is_match = primary_evidence and score >= 6 and not negative
    return TagClassification(
        tag="train",
        is_match=is_match,
        score=score,
        reason=unique_reason(reasons, "no train-object evidence found"),
    )


def classify_souterrains_card(card: CardRecord, metadata: WikipediaMetadata | None = None) -> TagClassification:
    """Match underground structures/places, excluding cultural or metaphorical uses."""

    visible_text = visible_topic_text(card)
    description_text, extract_text, category_text = metadata_topic_text(metadata)
    primary_text = " ".join([visible_text, description_text, category_text])
    score = 0
    reasons: list[str] = []

    visible_structure = phrase_matches(visible_text, UNDERGROUND_STRUCTURE_PHRASES)
    description_structure = phrase_matches(description_text, UNDERGROUND_STRUCTURE_PHRASES)
    category_structure = list(
        dict.fromkeys(
            phrase_matches(category_text, UNDERGROUND_CATEGORY_PHRASES)
            + phrase_matches(category_text, UNDERGROUND_STRUCTURE_PHRASES)
        )
    )
    extract_structure = phrase_matches(extract_text, UNDERGROUND_STRUCTURE_PHRASES)
    negative = phrase_matches(primary_text, UNDERGROUND_NEGATIVE_PHRASES)

    if visible_structure:
        score += 10
        reasons.append(f"visible underground structure term: {visible_structure[0]}")
    if description_structure:
        score += 8
        reasons.append(f"Wikipedia description underground structure term: {description_structure[0]}")
    if category_structure:
        score += 7
        reasons.append(f"Wikipedia category underground structure term: {category_structure[0]}")
    if extract_structure:
        score += 2
        reasons.append(f"Wikipedia extract underground structure support: {extract_structure[0]}")
    if negative:
        score -= 10
        reasons.append(f"excluded non-place underground context: {negative[0]}")

    primary_evidence = bool(visible_structure or description_structure or (category_structure and extract_structure))
    is_match = primary_evidence and score >= 6 and not negative
    return TagClassification(
        tag="souterrains",
        is_match=is_match,
        score=score,
        reason=unique_reason(reasons, "no underground structure/place evidence found"),
    )


def classify_riviere_card(card: CardRecord, metadata: WikipediaMetadata | None = None) -> TagClassification:
    """Match watercourses, canals, and wetlands, excluding adjacent or named-after topics."""

    visible_text = f"{card.subtitle} {' '.join(card.tags)}"
    description_text, extract_text, category_text = metadata_topic_text(metadata)
    negative_primary_text = " ".join([card.title, card.subtitle, description_text])
    score = 0
    reasons: list[str] = []

    visible_watercourse = phrase_matches(visible_text, RIVER_WATERCOURSE_PHRASES)
    description_watercourse = phrase_matches(description_text, RIVER_WATERCOURSE_PHRASES)
    category_watercourse = list(
        dict.fromkeys(
            phrase_matches(category_text, RIVER_CATEGORY_PHRASES)
            + phrase_matches(category_text, RIVER_WATERCOURSE_PHRASES)
        )
    )
    extract_watercourse = phrase_matches(extract_text, RIVER_WATERCOURSE_PHRASES)
    hard_negative = phrase_matches(negative_primary_text, RIVER_HARD_NEGATIVE_PHRASES)
    category_negative = phrase_matches(category_text, RIVER_CATEGORY_NEGATIVE_PHRASES)

    if visible_watercourse:
        score += 10
        reasons.append(f"visible river/watercourse term: {visible_watercourse[0]}")
    if description_watercourse:
        score += 8
        reasons.append(f"Wikipedia description river/watercourse term: {description_watercourse[0]}")
    if category_watercourse:
        score += 7
        reasons.append(f"Wikipedia category river/watercourse term: {category_watercourse[0]}")
    if extract_watercourse:
        score += 2
        reasons.append(f"Wikipedia extract river/watercourse support: {extract_watercourse[0]}")
    if hard_negative:
        score -= 20
        reasons.append(f"excluded non-river context: {hard_negative[0]}")
    if category_negative:
        score -= 8
        reasons.append(f"excluded non-river category: {category_negative[0]}")

    primary_evidence = bool(visible_watercourse or description_watercourse or (category_watercourse and extract_watercourse))
    is_match = primary_evidence and score >= 6 and not hard_negative and not category_negative
    return TagClassification(
        tag="rivière",
        is_match=is_match,
        score=score,
        reason=unique_reason(reasons, "no watercourse/canal/wetland evidence found"),
    )


def classify_a_bicrave_card(card: CardRecord, metadata: WikipediaMetadata | None = None) -> TagClassification:
    """Match low-rarity untagged cards that are intentionally marked for resale."""

    score = 0
    reasons: list[str] = []

    if card.rarity.upper() not in BICRAVE_LOW_RARITIES:
        reasons.append(f"rarity {card.rarity} is not C/PC")
        return TagClassification("à bicrave", False, score, unique_reason(reasons, "not low rarity"))

    bicrave_tag = normalized_tag("à bicrave")
    other_tags = [tag for tag in card.tags if normalized_tag(tag) != bicrave_tag]
    if other_tags:
        reasons.append(f"already has another tag: {', '.join(other_tags)}")
        return TagClassification("à bicrave", False, score, unique_reason(reasons, "already tagged"))

    visible_text = visible_topic_text(card)
    description_text, extract_text, category_text = metadata_text(metadata)
    primary_text = " ".join([visible_text, description_text, category_text])

    visible_topic = phrase_matches(visible_text, BICRAVE_TOPIC_PHRASES)
    description_topic = phrase_matches(description_text, BICRAVE_TOPIC_PHRASES)
    category_topic = list(
        dict.fromkeys(
            phrase_matches(category_text, BICRAVE_CATEGORY_PHRASES)
            + phrase_matches(category_text, BICRAVE_TOPIC_PHRASES)
        )
    )
    extract_topic = phrase_matches(extract_text, BICRAVE_TOPIC_PHRASES)
    negative = phrase_matches(primary_text, BICRAVE_NON_TARGET_PHRASES)
    hard_negative = phrase_matches(primary_text, BICRAVE_HARD_NEGATIVE_PHRASES)

    score += 3
    reasons.append(f"low rarity: {card.rarity}")
    reasons.append("no existing tags")

    if visible_topic:
        score += 10
        reasons.append(f"visible resale topic: {visible_topic[0]}")
    if description_topic:
        score += 8
        reasons.append(f"Wikipedia description resale topic: {description_topic[0]}")
    if category_topic:
        score += 6
        reasons.append(f"Wikipedia category resale topic: {category_topic[0]}")
    if extract_topic:
        score += 1
        reasons.append(f"Wikipedia extract resale topic: {extract_topic[0]}")
    if hard_negative:
        score -= 20
        reasons.append(f"excluded non-target context: {hard_negative[0]}")
    elif negative and not (visible_topic or description_topic):
        score -= 5
        reasons.append(f"weak category-only non-target context: {negative[0]}")

    primary_evidence = bool(visible_topic or description_topic or (category_topic and extract_topic))
    is_match = primary_evidence and score >= 9 and not hard_negative
    return TagClassification(
        tag="à bicrave",
        is_match=is_match,
        score=score,
        reason=unique_reason(reasons, "no resale-topic evidence found"),
    )


TAG_DEFINITIONS: dict[str, TagDefinition] = {
    "plante": TagDefinition("plante", "actual plant taxa only", classify_plante_card),
    "philo": TagDefinition("philo", "core philosophy people, schools, concepts, works, and institutions", classify_philo_card),
    "scam": TagDefinition("scam", "central scams, fraud cases, Ponzi schemes, fraudsters, and fraudulent organizations", classify_scam_card),
    "train": TagDefinition("train", "train objects only, including trains, locomotives, rolling stock, types, classes, and models", classify_train_card),
    "rivière": TagDefinition("rivière", "watercourses, canals, and wetlands only", classify_riviere_card),
    "souterrains": TagDefinition("souterrains", "underground structures and places only", classify_souterrains_card),
    "à bicrave": TagDefinition(
        "à bicrave",
        "untagged C/PC cards in low-value resale topics",
        classify_a_bicrave_card,
    ),
}


def classify_card_for_tag(tag: str, card: CardRecord, metadata: WikipediaMetadata | None = None) -> TagClassification:
    """Dispatch one card to the classifier registered for a supported tag."""

    try:
        definition = TAG_DEFINITIONS[tag]
    except KeyError as exc:
        raise ValueError(f"Unsupported tag: {tag}") from exc
    return definition.classifier(card, metadata)


def parse_tag_list_arg(value: str, option_name: str) -> tuple[str, ...]:
    """Parse a comma-separated tag list and reject unsupported tags early."""

    canonical_by_normalized = {normalize_text(tag): tag for tag in TAG_DEFINITIONS}
    requested = tuple(
        dict.fromkeys(
            canonical_by_normalized.get(normalize_text(part.strip()), part.strip())
            for part in value.split(",")
            if part.strip()
        )
    )
    if not requested:
        raise ValueError(f"{option_name} cannot be empty.")

    unsupported = [tag for tag in requested if tag not in TAG_DEFINITIONS]
    if unsupported:
        supported = ", ".join(SUPPORTED_TAGS)
        raise ValueError(f"Unsupported tag(s): {', '.join(unsupported)}. Supported tags: {supported}.")
    return requested


def parse_tags_arg(value: str) -> tuple[str, ...]:
    """Parse the inclusive tag CLI option."""

    return parse_tag_list_arg(value, "--tags")


def parse_exclude_tags_arg(value: str) -> tuple[str, ...]:
    """Parse the excluded tag CLI option."""

    return parse_tag_list_arg(value, "--exclude-tags")


def resolve_enabled_tags(
    tags_arg: str | None,
    target_tag_arg: str | None,
    exclude_tags_arg: str | None = None,
) -> tuple[str, ...]:
    """Resolve selected tags, then remove any requested exclusions."""

    if tags_arg and target_tag_arg:
        raise ValueError("Use either --tags or the legacy --target-tag option, not both.")

    selected_tags = parse_tags_arg(target_tag_arg if target_tag_arg else (tags_arg or DEFAULT_TAGS))
    excluded_tags = parse_exclude_tags_arg(exclude_tags_arg) if exclude_tags_arg is not None else ()
    if not excluded_tags:
        return selected_tags

    excluded = set(excluded_tags)
    enabled_tags = tuple(tag for tag in selected_tags if tag not in excluded)
    if not enabled_tags:
        raise ValueError("No tags remain after --exclude-tags.")
    return enabled_tags


def parse_bulk_tag_result_text(text: str) -> BulkTagResult | None:
    """Parse the success counts shown after a bulk etiquette action."""

    normalized = normalize_text(text)
    tagged_match = re.search(r"(\d+)\s+cartes?\s+etiquetees?", normalized)
    already_match = re.search(r"(\d+)\s+(?:cartes?\s+)?deja\s+etiquetees?", normalized)

    if not tagged_match and not already_match:
        return None

    return BulkTagResult(
        tagged=int(tagged_match.group(1)) if tagged_match else 0,
        already_tagged=int(already_match.group(1)) if already_match else 0,
        raw_text=re.sub(r"\s+", " ", text).strip(),
    )


def parse_selected_count_text(text: str) -> int | None:
    """Parse the collection selection toolbar count, when it is visible."""

    match = re.search(r"(\d+)\s+cartes?\s+selectionnees?", normalize_text(text))
    return int(match.group(1)) if match else None


def card_to_json(card: CardRecord) -> dict[str, Any]:
    return {
        "key": card.key,
        "title": card.title,
        "subtitle": card.subtitle,
        "rarity": card.rarity,
        "tags": list(card.tags),
        "visible_text": card.visible_text,
        "page_number": card.page_number,
        "page_total": card.page_total,
    }


def card_from_json(payload: dict[str, Any]) -> CardRecord:
    return CardRecord(
        key=str(payload.get("key") or ""),
        title=str(payload.get("title") or ""),
        subtitle=str(payload.get("subtitle") or ""),
        rarity=str(payload.get("rarity") or ""),
        tags=tuple(str(tag) for tag in payload.get("tags", [])),
        visible_text=str(payload.get("visible_text") or ""),
        page_number=int(payload["page_number"]) if payload.get("page_number") is not None else None,
        page_total=int(payload["page_total"]) if payload.get("page_total") is not None else None,
    )


def load_confirmed_tag_keys(path: Path = DEFAULT_CONFIRMED_TAGS_PATH) -> dict[str, set[str]]:
    """Load card/tag pairs confirmed by previous WikiMasters bulk results."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        log(f"Could not read confirmed tag cache {path}: {exc}")
        return {}

    raw_tags = payload.get("tags", {})
    if not isinstance(raw_tags, dict):
        return {}

    canonical_by_normalized = {normalized_tag(tag): tag for tag in TAG_DEFINITIONS}
    confirmed: dict[str, set[str]] = {}
    for raw_tag, raw_keys in raw_tags.items():
        canonical_tag = canonical_by_normalized.get(normalized_tag(str(raw_tag)))
        if canonical_tag is None or not isinstance(raw_keys, list):
            continue
        confirmed[canonical_tag] = {str(key) for key in raw_keys if str(key)}
    return confirmed


def save_confirmed_tag_keys(
    confirmed: dict[str, set[str]],
    path: Path = DEFAULT_CONFIRMED_TAGS_PATH,
) -> None:
    """Persist confirmed card/tag pairs used to stabilize later dry runs."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tags": {tag: sorted(keys) for tag, keys in sorted(confirmed.items()) if keys},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record_confirmed_tags(
    target_tag: str,
    cards: Sequence[CardRecord],
    path: Path = DEFAULT_CONFIRMED_TAGS_PATH,
) -> None:
    """Remember card/tag pairs after WikiMasters reports tagged/already-tagged."""

    if not cards:
        return
    confirmed = load_confirmed_tag_keys(path)
    tag_keys = confirmed.setdefault(target_tag, set())
    before_count = len(tag_keys)
    tag_keys.update(card.key for card in cards if card.key)
    if len(tag_keys) != before_count:
        save_confirmed_tag_keys(confirmed, path)


def forget_confirmed_tags(
    target_tag: str,
    cards: Sequence[CardRecord],
    path: Path = DEFAULT_CONFIRMED_TAGS_PATH,
) -> None:
    """Remove local confirmations after this script removes a tag."""

    confirmed = load_confirmed_tag_keys(path)
    tag_keys = confirmed.get(target_tag)
    if not tag_keys:
        return
    before_count = len(tag_keys)
    for card in cards:
        tag_keys.discard(card.key)
    if len(tag_keys) != before_count:
        if tag_keys:
            confirmed[target_tag] = tag_keys
        else:
            confirmed.pop(target_tag, None)
        save_confirmed_tag_keys(confirmed, path)


def apply_confirmed_tags(
    cards: Sequence[CardRecord],
    confirmed: dict[str, set[str]],
    enabled_tags: Sequence[str],
) -> tuple[list[CardRecord], int]:
    """Patch scanned cards with locally confirmed tags missing from grid text."""

    updated_cards: list[CardRecord] = []
    added_count = 0
    for card in cards:
        tags = list(card.tags)
        for tag in enabled_tags:
            if card.key not in confirmed.get(tag, set()) or has_target_tag(card, tag):
                continue
            tags.append(tag)
            added_count += 1
        if len(tags) == len(card.tags):
            updated_cards.append(card)
        else:
            updated_cards.append(replace(card, tags=tuple(dict.fromkeys(tags))))
    return updated_cards, added_count


def build_candidates_by_tag(
    cards: Sequence[CardRecord],
    classifications: dict[str, dict[str, TagClassification]],
    tags: Sequence[str],
) -> dict[str, list[CardRecord]]:
    candidates_by_tag: dict[str, list[CardRecord]] = {tag: [] for tag in tags}
    assigned_card_keys: set[str] = set()
    for tag in tags:
        for card in cards:
            if has_any_tag(card) or card.key in assigned_card_keys:
                continue
            classification = classifications.get(tag, {}).get(card.key, TagClassification(tag, False, 0, ""))
            if not classification.is_match:
                continue
            candidates_by_tag[tag].append(card)
            assigned_card_keys.add(card.key)
    return candidates_by_tag


def build_invalid_existing_by_tag(
    cards: Sequence[CardRecord],
    classifications: dict[str, dict[str, TagClassification]],
    tags: Sequence[str],
) -> dict[str, list[CardRecord]]:
    """Return already-tagged cards that no longer satisfy their classifier."""

    return {
        tag: [
            card
            for card in cards
            if has_target_tag(card, tag)
            and not classifications.get(tag, {}).get(card.key, TagClassification(tag, False, 0, "")).is_match
        ]
        for tag in tags
    }


def write_candidate_artifact(
    path: Path,
    cards: Sequence[CardRecord],
    classifications: dict[str, dict[str, TagClassification]],
    tags: Sequence[str],
) -> None:
    """Write reusable candidates so apply retries can skip full rescans."""

    path.parent.mkdir(parents=True, exist_ok=True)
    candidates_by_tag = build_candidates_by_tag(cards, classifications, tags)
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cards_scanned": len(cards),
        "tags": list(tags),
        "candidates": {
            tag: [
                {
                    "card": card_to_json(card),
                    "classification": {
                        "score": classifications[tag][card.key].score,
                        "reason": classifications[tag][card.key].reason,
                    },
                }
                for card in candidates_by_tag[tag]
            ]
            for tag in tags
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_candidate_artifact(path: Path, requested_tags: Sequence[str] | None = None) -> CandidateArtifact:
    """Load candidates produced by a previous scan/dry run."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Candidate file not found: {path}. Run a dry run first.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Candidate file is not valid JSON: {path}") from exc

    if int(payload.get("version", 0)) != 1:
        raise RuntimeError(f"Unsupported candidate file version in {path}.")

    raw_candidates = payload.get("candidates", {})
    if not isinstance(raw_candidates, dict):
        raise RuntimeError(f"Candidate file is missing a candidates object: {path}")

    available_tags = tuple(tag for tag in payload.get("tags", raw_candidates.keys()) if tag in raw_candidates)
    tags = tuple(tag for tag in (requested_tags or available_tags) if tag in raw_candidates)
    if not tags:
        requested = ", ".join(requested_tags or ())
        available = ", ".join(available_tags)
        raise RuntimeError(f"No saved candidates for requested tag(s): {requested or 'none'}. Available: {available or 'none'}.")

    cards_by_tag: dict[str, list[CardRecord]] = {}
    classifications: dict[str, dict[str, TagClassification]] = {}
    for tag in tags:
        cards_by_tag[tag] = []
        classifications[tag] = {}
        entries = raw_candidates.get(tag, [])
        if not isinstance(entries, list):
            raise RuntimeError(f"Candidate list for tag '{tag}' is malformed in {path}.")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("card"), dict):
                raise RuntimeError(f"Candidate entry for tag '{tag}' is malformed in {path}.")
            card = card_from_json(entry["card"])
            classification_payload = entry.get("classification", {})
            classification = TagClassification(
                tag=tag,
                is_match=True,
                score=int(classification_payload.get("score", 0)),
                reason=str(classification_payload.get("reason") or "loaded from saved candidates"),
            )
            cards_by_tag[tag].append(card)
            classifications[tag][card.key] = classification

    return CandidateArtifact(
        tags=tags,
        cards_by_tag=cards_by_tag,
        classifications=classifications,
        cards_scanned=int(payload.get("cards_scanned", 0)),
        generated_at=str(payload.get("generated_at") or ""),
    )


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def display_rarity(rarity: str) -> str:
    normalized = rarity.upper() if RARITY_PATTERN.fullmatch(rarity or "") else "unknown"
    return f"{RARITY_EMOJIS.get(normalized, RARITY_EMOJIS['unknown'])} {normalized}"


def render_tagged_cards_summary(applied: dict[str, Sequence[CardRecord]], tags: Sequence[str]) -> list[str]:
    """Render the GitHub Actions summary for non-bicrave cards tagged in this run."""

    bicrave_tag = normalized_tag("à bicrave")
    applied_by_tag = {
        tag: list(applied.get(tag, ()))
        for tag in tags
        if normalized_tag(tag) != bicrave_tag and applied.get(tag)
    }
    total_applied = sum(len(cards) for cards in applied_by_tag.values())
    lines = [
        "# WikiMasters Tagged Cards",
        "",
        f"- Non-`à bicrave` card/tag additions applied this run: {total_applied}",
        "",
    ]
    if not total_applied:
        lines.append("No non-`à bicrave` cards were tagged in this run.")
        return lines

    for tag, cards in applied_by_tag.items():
        lines.extend(
            [
                f"## `{tag}`",
                "",
                "| Card | Rarity |",
                "|---|---:|",
            ]
        )
        for card in cards:
            lines.append(f"| {markdown_cell(card.title)} | {markdown_cell(display_rarity(card.rarity))} |")
        lines.append("")
    return lines


def write_github_tagged_summary(applied: dict[str, Sequence[CardRecord]], tags: Sequence[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write("\n".join(render_tagged_cards_summary(applied, tags)) + "\n")
    except OSError as exc:
        log(f"Could not write GitHub step summary: {exc}")


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


def save_failure_artifacts(page: Page | None, reason: str, detail: str | None = None) -> None:
    """Write a small text file and screenshot for debugging failed browser runs."""

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "-", reason).strip("-")[:60] or "failure"
    metadata = ARTIFACT_DIR / f"{safe_reason}.txt"
    metadata.write_text(
        "\n".join(
            [
                f"reason={reason}",
                f"timestamp={datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                f"url={page.url if page else 'unknown'}",
                f"detail={detail or ''}",
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
    ui_pause(page, 500)


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
    """Authenticate if WikiMasters shows the login page."""

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
        ui_pause(page, 250)

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
    """Abort image/media/font requests so collection scans stay lightweight."""

    if route.request.resource_type in {"image", "media", "font"}:
        route.abort()
        return
    route.continue_()


def extract_visible_cards(page: Page) -> list[CardRecord]:
    """Read currently visible card-shaped DOM nodes from the collection grid."""

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

          // Cards do not currently expose stable data attributes, so the
          // scraper recognizes visible card-sized nodes that include a rarity.
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
    """Return scroll state for the collection's inner scroll container."""

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
    """Advance the collection's inner scroll container by roughly one viewport."""

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
    """Click the collection next-page button and wait until cards change."""

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
        ui_pause(page, 250)
        counter = read_collection_page_counter(page)
        signature = visible_card_signature(page)
        if previous_counter and counter and counter[0] != previous_counter[0]:
            reset_collection_scroll(page)
            ui_pause(page, 300)
            return True
        if not previous_counter and signature and signature != previous_signature:
            reset_collection_scroll(page)
            ui_pause(page, 300)
            return True

    return False


def go_to_collection_page(page: Page, target_page: int | None) -> None:
    """Move from a fresh collection load to the page recorded during scanning."""

    if not target_page or target_page <= 1:
        reset_collection_scroll(page)
        return

    current_page = 1
    counter = read_collection_page_counter(page)
    if counter:
        current_page = counter[0]

    while current_page < target_page:
        if not click_next_collection_page(page):
            raise RuntimeError(f"Could not navigate to collection page {target_page}; stopped at page {current_page}.")
        counter = read_collection_page_counter(page)
        current_page = counter[0] if counter else current_page + 1

    if current_page != target_page:
        raise RuntimeError(f"Expected collection page {target_page}, but reached page {current_page}.")

    reset_collection_scroll(page)
    ui_pause(page, 300)


def nearby_page_numbers(page_number: int | None, page_total: int | None, radius: int = 8) -> tuple[int, ...]:
    """Return pages to retry when a card has drifted from its recorded page."""

    if not page_number:
        return ()

    pages: list[int] = []
    offsets = [0]
    for distance in range(1, radius + 1):
        offsets.extend([-distance, distance])

    for offset in offsets:
        candidate = page_number + offset
        if candidate < 1:
            continue
        if page_total is not None and candidate > page_total:
            continue
        if candidate not in pages:
            pages.append(candidate)
    return tuple(pages)


def scan_collection_page(page: Page, max_cards: int, scroll_delay_ms: int) -> list[CardRecord]:
    """Collect all cards visible on one paginated collection page."""

    records: dict[str, CardRecord] = {}
    stagnant_rounds = 0
    initial_deadline = time.monotonic() + 15

    reset_collection_scroll(page)
    ui_pause(page, scroll_delay_ms)

    while time.monotonic() < initial_deadline:
        initial_cards = extract_visible_cards(page)
        if initial_cards:
            for card in initial_cards:
                records.setdefault(card.key, card)
            break
        ui_pause(page, 500)

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
        ui_pause(page, scroll_delay_ms)
        if metrics["scrollTop"] == previous_scroll_top:
            stagnant_rounds += 1

    return list(records.values())[:max_cards or None]


def scan_collection(page: Page, max_cards: int, scroll_delay_ms: int) -> list[CardRecord]:
    """Scan every collection page once, respecting --max-cards when provided."""

    records: dict[str, CardRecord] = {}
    visited_pages: set[tuple[int, int]] = set()
    inferred_page_number = 1

    while True:
        counter = read_collection_page_counter(page)
        if counter:
            if counter in visited_pages:
                log(f"Already scanned {page_label(counter)}; stopping to avoid a pagination loop.")
                break
            visited_pages.add(counter)
            inferred_page_number = counter[0]

        remaining = max_cards - len(records) if max_cards > 0 else 0
        log(f"Scanning collection {page_label(counter)}.")
        page_cards = scan_collection_page(page, remaining, scroll_delay_ms)

        before_total = len(records)
        for card in page_cards:
            card_with_page = replace(
                card,
                page_number=counter[0] if counter else inferred_page_number,
                page_total=counter[1] if counter else None,
            )
            records.setdefault(card.key, card_with_page)
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
        inferred_page_number += 1

    return list(records.values())[:max_cards or None]


class WikipediaClient:
    """Batched, cached wrapper around the French Wikipedia query API."""

    def __init__(self, cache_path: Path, delay_ms: int, batch_size: int, timeout_seconds: int = 15) -> None:
        self.cache_path = cache_path
        self.delay_ms = delay_ms
        self.batch_size = max(1, min(batch_size, 25))
        self.timeout_seconds = timeout_seconds
        self.cache: dict[str, dict[str, Any]] = self._load_cache()
        self.description_prop_supported = True

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        """Load cached page metadata, falling back to the old plant cache."""

        path = self.cache_path
        if path == DEFAULT_CACHE_PATH and not path.exists() and LEGACY_CACHE_PATH.exists():
            path = LEGACY_CACHE_PATH
            log(f"Loading legacy Wikipedia cache {LEGACY_CACHE_PATH}; future writes use {self.cache_path}.")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            log(f"Could not read Wikipedia cache {path}: {exc}")
            return {}

    def save_cache(self) -> None:
        """Persist metadata after every fetched batch to survive interrupted runs."""

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def fetch_many(self, titles: Sequence[str]) -> dict[str, WikipediaMetadata]:
        """Fetch uncached titles in throttled batches and return all requested data."""

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
        """Fetch one Wikipedia API batch and normalize redirects/missing pages."""

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
                "User-Agent": "project-wikimaster-topic-tagger/1.0 (https://www.wiki-masters.com)",
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
    ui_pause(page, max(delay_ms, 1_200))


def clear_collection_filter(page: Page, delay_ms: int) -> None:
    search = find_collection_search(page)
    if search is None:
        return
    select_all_text(search)
    search.press("Backspace")
    ui_pause(page, delay_ms)


def search_queries_for_card(card: CardRecord) -> tuple[str, ...]:
    """Return progressively broader collection-search queries for one title."""

    title = card.title.strip()
    queries = [title]

    no_parenthetical = re.sub(r"\s*\([^)]*\)", "", title).strip()
    if no_parenthetical:
        queries.append(no_parenthetical)

    for separator in (",", ":", " - ", " – "):
        if separator in title:
            prefix = title.split(separator, 1)[0].strip()
            if prefix:
                queries.append(prefix)

    normalized = normalize_text(title)
    if normalized:
        queries.append(normalized)
        tokens = normalized.split()
        if len(tokens) >= 2:
            queries.append(" ".join(tokens[: min(4, len(tokens))]))

    return tuple(dict.fromkeys(query for query in queries if len(query) >= 3))


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
    ui_pause(page, 500)


def find_card_click_point_by_scrolling(
    page: Page,
    card: CardRecord,
    delay_ms: int,
    selection_mode: bool = False,
) -> dict[str, float] | None:
    """Search the current page's scroll container for a card click point."""

    reset_collection_scroll(page)
    ui_pause(page, delay_ms)
    stagnant_rounds = 0

    while True:
        point = find_visible_card_click_point(page, card, selection_mode=selection_mode)
        if point is not None:
            return point

        metrics = read_collection_scroll_metrics(page)
        if metrics["atBottom"] and stagnant_rounds >= 2:
            return None

        previous_scroll_top = metrics["scrollTop"]
        metrics = scroll_collection(page)
        ui_pause(page, delay_ms)
        if metrics["scrollTop"] == previous_scroll_top:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0


def find_visible_card_click_point(page: Page, card: CardRecord, selection_mode: bool = False) -> dict[str, float] | None:
    """Find the checkbox/card center used to select one searched card."""

    return page.evaluate(
        """
        ({ title, subtitle, rarity, selectionMode }) => {
          const normalize = (value) =>
            (value || '')
              .normalize('NFD')
              .replace(/[\\u0300-\\u036f]/g, '')
              .toLowerCase()
              .replace(/\\s+/g, ' ')
              .trim();
          const titleNorm = normalize(title);
          const subtitleNorm = normalize(subtitle);
          const rarityNorm = normalize(rarity);
          const rarityPattern = /^(L|UR|SR|R|PC|C)$/i;
          const viewportTop = 80;
          const viewportBottom = window.innerHeight - 90;
          const isVisible = (element, rect) => {
            const style = window.getComputedStyle(element);
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
          const selectionPointFor = (element, cardRect) => {
            const controls = [
              ...element.querySelectorAll('input[type="checkbox"], [role="checkbox"], button, [role="button"]'),
            ]
              .map((control) => ({ control, rect: control.getBoundingClientRect() }))
              .filter(({ control, rect }) => {
                if (!isVisible(control, rect)) return false;
                if (rect.width < 12 || rect.width > 52 || rect.height < 12 || rect.height > 52) return false;
                if (rect.left < cardRect.right - 70 || rect.right > cardRect.right + 8) return false;
                if (rect.top < cardRect.top - 8 || rect.top > cardRect.top + 78) return false;
                if (rect.bottom < viewportTop || rect.top > viewportBottom) return false;
                return true;
              })
              .map(({ rect }) => ({
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
                score: 1000 - Math.abs(rect.right - cardRect.right) - Math.abs(rect.top - cardRect.top),
              }))
              .sort((a, b) => b.score - a.score);
            if (controls.length) return controls[0];
            return {
              x: cardRect.right - Math.min(18, cardRect.width * 0.12),
              y: Math.min(
                Math.max(cardRect.top + Math.min(28, cardRect.height * 0.14), viewportTop),
                viewportBottom
              ),
              score: 0,
            };
          };
          const candidates = [];

          for (const element of document.querySelectorAll('article, a, button, [role="button"], [role="listitem"], div')) {
            const rect = element.getBoundingClientRect();
            if (
              !isVisible(element, rect) ||
              rect.width < 110 ||
              rect.width > 280 ||
              rect.height < 150 ||
              rect.height > 380 ||
              rect.bottom < viewportTop ||
              rect.top > viewportBottom
            ) {
              continue;
            }

            const lines = (element.innerText || '').split('\\n').map((line) => normalize(line)).filter(Boolean);
            if (!lines.some((line) => rarityPattern.test(line))) continue;
            if (rarityNorm && !lines.some((line) => line === rarityNorm)) continue;
            const hasTitle = lines.some((line) => line === titleNorm || line.includes(titleNorm) || titleNorm.includes(line));
            if (!hasTitle) continue;
            const hasSubtitle = !subtitleNorm || lines.some((line) => line === subtitleNorm || line.includes(subtitleNorm));
            const selectionPoint = selectionMode ? selectionPointFor(element, rect) : null;
            const score =
              (hasSubtitle ? 1000 : 0) +
              (selectionPoint && selectionPoint.score > 0 ? 700 : 0) +
              Math.min((rect.width * rect.height) / 100, 500) -
              rect.top / 10;
            candidates.push({ element, rect, selectionPoint, score });
          }

          candidates.sort((a, b) => b.score - a.score);
          const candidate = candidates[0];
          if (!candidate) return null;

          if (selectionMode) {
            return { x: candidate.selectionPoint.x, y: candidate.selectionPoint.y };
          }

          const visibleTop = Math.max(candidate.rect.top, viewportTop);
          const visibleBottom = Math.min(candidate.rect.bottom, viewportBottom);
          return {
            x: candidate.rect.left + candidate.rect.width / 2,
            y: visibleTop + (visibleBottom - visibleTop) / 2,
          };
        }
        """,
        {"title": card.title, "subtitle": card.subtitle, "rarity": card.rarity, "selectionMode": selection_mode},
    )


def read_selected_count(page: Page) -> int | None:
    """Return the bulk-selection toolbar count, or None if it is not visible."""

    try:
        return parse_selected_count_text(page.locator("body").inner_text(timeout=1_000))
    except PlaywrightError:
        return None


def selected_count_is_at_least(page: Page, expected_count: int, timeout_ms: int = 1_000) -> tuple[bool, int | None]:
    """Return whether the bulk-selection count is visible and high enough."""

    deadline = time.monotonic() + timeout_ms / 1_000
    last_count = read_selected_count(page)
    while True:
        if last_count is not None and last_count >= expected_count:
            return True, last_count
        if time.monotonic() >= deadline:
            return False, last_count
        ui_pause(page, 150)
        last_count = read_selected_count(page)


def wait_for_selected_count_increase(page: Page, previous_count: int, timeout_ms: int = 2_000) -> int | None:
    """Poll until the WikiMasters toolbar confirms one more selected card."""

    deadline = time.monotonic() + timeout_ms / 1_000
    last_count = read_selected_count(page)
    while time.monotonic() < deadline:
        if last_count is not None and last_count > previous_count:
            is_stable, stable_count = selected_count_is_at_least(page, last_count, timeout_ms=350)
            if is_stable:
                return stable_count
            last_count = stable_count
            continue
        ui_pause(page, 150)
        last_count = read_selected_count(page)
    return last_count if last_count is not None and last_count > previous_count else None


def select_card(page: Page, card: CardRecord, selection_delay_ms: int, force_search: bool = False) -> bool:
    """Select one card and return whether a search filter was needed."""

    point = None if force_search else find_card_click_point_by_scrolling(page, card, selection_delay_ms, selection_mode=True)
    used_filter = False
    if point is None:
        for query in search_queries_for_card(card):
            filter_collection(page, query, selection_delay_ms)
            used_filter = True
            point = find_card_click_point_by_scrolling(
                page,
                card,
                max(selection_delay_ms, 500),
                selection_mode=True,
            )
            if point is not None:
                break
    if point is None:
        page_hint = f" on page {card.page_number}" if card.page_number else ""
        raise RuntimeError(f"Could not find visible card to select{page_hint}: {card.title}")

    before_count = read_selected_count(page)
    if before_count is None:
        raise RuntimeError(f"Selection toolbar count is not visible before selecting: {card.title}")

    for attempt in range(2):
        if attempt > 0:
            point = find_visible_card_click_point(page, card, selection_mode=True)
            if point is None:
                break
        page.mouse.click(point["x"], point["y"])
        ui_pause(page, selection_delay_ms)
        after_count = wait_for_selected_count_increase(page, before_count)
        if after_count is not None:
            return used_filter

    if used_filter:
        clear_collection_filter(page, selection_delay_ms)

    current_count = read_selected_count(page)
    count_hint = "unknown" if current_count is None else str(current_count)
    raise RuntimeError(
        f"Click did not select card: {card.title}; selection count stayed at {before_count} "
        f"(current: {count_hint})."
    )


def select_card_with_page_fallback(page: Page, card: CardRecord, selection_delay_ms: int) -> tuple[int | None, bool]:
    """Select a card, retrying nearby pages if pagination shifted after mutations."""

    tried_pages = [card.page_number] if card.page_number else []
    try:
        used_filter = select_card(page, card, selection_delay_ms)
        return card.page_number, used_filter
    except RuntimeError as first_error:
        last_error = first_error

    for fallback_page in nearby_page_numbers(card.page_number, card.page_total):
        if fallback_page == card.page_number:
            continue
        try:
            page.goto(COLLECTION_URL, wait_until="domcontentloaded")
            settle_page(page)
            go_to_collection_page(page, fallback_page)
            click_select_mode(page)
            clear_collection_filter(page, selection_delay_ms)
            used_filter = select_card(page, card, selection_delay_ms)
            log(f"Found '{card.title}' on page {fallback_page} after recorded page {card.page_number}.")
            return fallback_page, used_filter
        except RuntimeError as exc:
            tried_pages.append(fallback_page)
            last_error = exc

    tried = ", ".join(str(page_number) for page_number in tried_pages if page_number is not None)
    raise RuntimeError(f"{last_error} Tried nearby pages: {tried or 'none'}.") from last_error


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
    """Open the bulk etiquette action using visible text/labels."""

    button_pattern = re.compile(r"^étiqueter$|^etiqueter$|ajouter.*étiquette|ajouter.*etiquette|add.*tag", re.IGNORECASE)
    button = get_first_visible(
        [
            page.get_by_role("button", name=button_pattern),
        ],
        timeout_ms=800,
    )
    if button is not None:
        try:
            if button.is_enabled(timeout=500):
                button.click(timeout=3_000)
                ui_pause(page, 600)
                return
        except PlaywrightError:
            pass

    action = page.evaluate(
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
            if (style.visibility === 'hidden' || style.display === 'none' || rect.width === 0 || rect.height === 0) {
              continue;
            }
            const text = normalize([
              element.innerText,
              element.getAttribute('aria-label'),
              element.getAttribute('title'),
            ].filter(Boolean).join(' '));
            if (!/(etiquet|tag)/.test(text)) continue;
            if (/toutes les etiquettes|filtre/.test(text)) continue;
            const disabled =
              element.disabled ||
              element.getAttribute('aria-disabled') === 'true' ||
              element.hasAttribute('disabled') ||
              style.pointerEvents === 'none';
            const score =
              (/etiqueter|ajouter|add/.test(text) ? 1000 : 0) +
              (/etiquet|tag/.test(text) ? 200 : 0) -
              rect.top;
            candidates.push({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, score, text, disabled });
          }
          candidates.sort((a, b) => b.score - a.score);
          const enabled = candidates.find((candidate) => !candidate.disabled);
          return { enabled: enabled || null, disabledCount: candidates.filter((candidate) => candidate.disabled).length };
        }
        """
    )
    point = action.get("enabled") if isinstance(action, dict) else None
    if not point:
        selected_count = read_selected_count(page)
        disabled_count = int(action.get("disabledCount", 0)) if isinstance(action, dict) else 0
        if disabled_count and (selected_count is None or selected_count <= 0):
            raise SelectionLostError(
                "Bulk étiquette action is visible but disabled because no cards are selected."
            )
        raise RuntimeError("Could not find a bulk étiquette action.\n" + dump_visible_controls(page))
    page.mouse.click(point["x"], point["y"])
    ui_pause(page, 600)


def click_tag_option_or_fill(page: Page, target_tag: str) -> None:
    """Choose an existing tag option or create it from the bulk-tag modal."""

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
        ui_pause(page, 500)
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
    ui_pause(page, 300)
    tag_input.press("Enter")
    ui_pause(page, 500)

    create_option = get_first_visible(
        [
            page.get_by_role("button", name=re.compile(rf"créer.*{re.escape(target_tag)}|creer.*{re.escape(target_tag)}", re.IGNORECASE)),
            page.get_by_role("option", name=re.compile(rf"créer.*{re.escape(target_tag)}|creer.*{re.escape(target_tag)}", re.IGNORECASE)),
            page.get_by_role("menuitem", name=re.compile(rf"créer.*{re.escape(target_tag)}|creer.*{re.escape(target_tag)}", re.IGNORECASE)),
            page.get_by_text(re.compile(rf"créer.*{re.escape(target_tag)}|creer.*{re.escape(target_tag)}", re.IGNORECASE)),
        ],
        timeout_ms=1_000,
    )
    if create_option is not None:
        create_option.click(timeout=3_000)
        ui_pause(page, 700)


def read_bulk_tag_result(page: Page) -> BulkTagResult | None:
    try:
        body_text = page.locator("body").inner_text(timeout=1_000)
    except PlaywrightError:
        return None
    return parse_bulk_tag_result_text(body_text)


def wait_for_bulk_tag_result(page: Page, timeout_ms: int = 5_000) -> BulkTagResult | None:
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        result = read_bulk_tag_result(page)
        if result is not None:
            return result
        ui_pause(page, 150)
    return read_bulk_tag_result(page)


def click_confirmation_if_present(page: Page) -> None:
    """Click an apply-style confirmation button if the bulk-tag modal shows one."""

    confirm = get_first_visible(
        [
            page.get_by_role(
                "button",
                name=re.compile("appliquer|ajouter|retirer|enlever|supprimer|enregistrer|valider|confirmer", re.IGNORECASE),
            ),
            page.get_by_text(re.compile("appliquer|ajouter|retirer|enlever|supprimer|enregistrer|valider|confirmer", re.IGNORECASE)),
        ],
        timeout_ms=800,
    )
    if confirm is not None:
        try:
            if confirm.is_enabled(timeout=500):
                confirm.click(timeout=3_000)
                ui_pause(page, 700)
        except PlaywrightError:
            pass


def click_bulk_result_close(page: Page) -> None:
    """Close the bulk-tag result modal if it is visible."""

    close_button = get_first_visible(
        [
            page.get_by_role("button", name=re.compile(r"^terminé$|^termine$|^ok$|^fermer$", re.IGNORECASE)),
            page.get_by_text(re.compile(r"^terminé$|^termine$|^ok$|^fermer$", re.IGNORECASE)),
        ],
        timeout_ms=1_500,
    )
    if close_button is not None:
        try:
            close_button.click(timeout=3_000)
            ui_pause(page, 700)
            return
        except PlaywrightError:
            pass

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
          const isVisible = (element, rect) => {
            const style = window.getComputedStyle(element);
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
              const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')]
            .map((element) => {
              const rect = element.getBoundingClientRect();
              if (!isVisible(element, rect)) return null;
              const text = normalize(element.innerText || '');
              if (!/(carte etiquetee|deja etiquetee|appliquer une etiquette|retirer une etiquette|etiquette retiree|cartes? modifiees?|mise a jour)/.test(text)) return null;
              return { element, rect, area: rect.width * rect.height };
            })
            .filter(Boolean)
            .sort((a, b) => a.area - b.area);
          const dialog = dialogs[0]?.element;
          if (!dialog) return null;

          const buttons = [...dialog.querySelectorAll('button, [role="button"], a')]
            .map((element) => {
              const rect = element.getBoundingClientRect();
              if (!isVisible(element, rect)) return null;
              const text = normalize([
                element.innerText,
                element.getAttribute('aria-label'),
                element.getAttribute('title'),
              ].filter(Boolean).join(' '));
              const score =
                (/termine|fermer|close|ok/.test(text) ? 1000 : 0) +
                (rect.top < dialog.getBoundingClientRect().top + 80 && rect.left > dialog.getBoundingClientRect().right - 90 ? 300 : 0);
              return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, score };
            })
            .filter(Boolean)
            .sort((a, b) => b.score - a.score);
          return buttons[0] || null;
        }
        """
    )
    if point:
        page.mouse.click(point["x"], point["y"])
        ui_pause(page, 700)
        return

    try:
        page.keyboard.press("Escape")
        ui_pause(page, 700)
    except PlaywrightError:
        pass


def add_tag_to_selected(page: Page, target_tag: str, expected_count: int = 1) -> BulkTagResult | None:
    """Use the open bulk UI to add one etiquette to selected cards."""

    selected_enough, selected_count = selected_count_is_at_least(page, expected_count, timeout_ms=500)
    if not selected_enough:
        count_hint = "unknown" if selected_count is None else str(selected_count)
        raise SelectionLostError(
            f"Expected at least {expected_count} selected card(s) before tagging '{target_tag}', "
            f"but the UI shows {count_hint}."
        )

    click_bulk_tag_menu(page)
    click_tag_option_or_fill(page, target_tag)
    result = wait_for_bulk_tag_result(page, timeout_ms=2_000)
    if result is None:
        click_confirmation_if_present(page)
        result = wait_for_bulk_tag_result(page, timeout_ms=5_000)
    if result is not None:
        log(f"Bulk tag result: {result.tagged} tagged, {result.already_tagged} already tagged.")
        click_bulk_result_close(page)
        return result

    click_confirmation_if_present(page)
    return None


def same_card_identity(left: CardRecord, right: CardRecord) -> bool:
    """Compare scanned cards by stable visible identity rather than page position."""

    if normalize_text(left.title) != normalize_text(right.title):
        return False
    if left.rarity and right.rarity and left.rarity.upper() != right.rarity.upper():
        return False
    if left.subtitle and right.subtitle and normalize_text(left.subtitle) != normalize_text(right.subtitle):
        return False
    return True


def visible_card_has_tag(page: Page, card: CardRecord, target_tag: str) -> bool:
    for visible_card in extract_visible_cards(page):
        if same_card_identity(visible_card, card):
            return has_target_tag(visible_card, target_tag)
    return False


def card_has_tag_by_scrolling(page: Page, card: CardRecord, target_tag: str, delay_ms: int) -> bool:
    """Scan the current page's scroll container until the card/tag is found."""

    reset_collection_scroll(page)
    ui_pause(page, delay_ms)
    stagnant_rounds = 0

    while True:
        if visible_card_has_tag(page, card, target_tag):
            return True

        metrics = read_collection_scroll_metrics(page)
        if metrics["atBottom"] and stagnant_rounds >= 2:
            return False

        previous_scroll_top = metrics["scrollTop"]
        metrics = scroll_collection(page)
        ui_pause(page, delay_ms)
        if metrics["scrollTop"] == previous_scroll_top:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0


def verify_batch_tags(page: Page, batch: Sequence[CardRecord], target_tag: str, delay_ms: int) -> None:
    """Re-scan each selected card's page after applying and confirm the tag."""

    missing: list[str] = []
    for card in batch:
        found = False
        for _ in range(5):
            if card_has_tag_by_scrolling(page, card, target_tag, delay_ms):
                found = True
                break
            ui_pause(page, delay_ms)
        if not found:
            missing.append(card.title)

    if missing:
        raise RuntimeError(f"Tag verification failed for: {', '.join(missing)}")


def card_tag_status_by_search(page: Page, card: CardRecord, target_tag: str, delay_ms: int) -> bool | None:
    """Search for one card and return whether it still carries a target tag."""

    page.goto(COLLECTION_URL, wait_until="domcontentloaded")
    settle_page(page)
    filter_collection(page, card.title, delay_ms)
    reset_collection_scroll(page)
    ui_pause(page, delay_ms)
    stagnant_rounds = 0

    while True:
        for visible_card in extract_visible_cards(page):
            if same_card_identity(visible_card, card):
                return has_target_tag(visible_card, target_tag)

        metrics = read_collection_scroll_metrics(page)
        if metrics["atBottom"] and stagnant_rounds >= 2:
            return None

        previous_scroll_top = metrics["scrollTop"]
        metrics = scroll_collection(page)
        ui_pause(page, delay_ms)
        if metrics["scrollTop"] == previous_scroll_top:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0


def current_card_by_search(page: Page, card: CardRecord, delay_ms: int) -> CardRecord | None:
    """Search current collection state for one saved card candidate."""

    page.goto(COLLECTION_URL, wait_until="domcontentloaded")
    settle_page(page)
    for query in search_queries_for_card(card):
        filter_collection(page, query, delay_ms)
        reset_collection_scroll(page)
        ui_pause(page, delay_ms)
        stagnant_rounds = 0

        while True:
            for visible_card in extract_visible_cards(page):
                if same_card_identity(visible_card, card):
                    return visible_card

            metrics = read_collection_scroll_metrics(page)
            if metrics["atBottom"] and stagnant_rounds >= 2:
                break

            previous_scroll_top = metrics["scrollTop"]
            metrics = scroll_collection(page)
            ui_pause(page, delay_ms)
            if metrics["scrollTop"] == previous_scroll_top:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0

    return None


def filter_current_tagless_candidates(
    page: Page,
    candidates: Sequence[CardRecord],
    target_tag: str,
    delay_ms: int,
) -> list[CardRecord]:
    """Keep saved candidates whose current visible collection card still has no tags."""

    tagless: list[CardRecord] = []
    skipped_tagged = 0
    skipped_missing = 0
    for card in candidates:
        current_card = current_card_by_search(page, card, delay_ms)
        if current_card is None:
            skipped_missing += 1
            continue
        if has_any_tag(current_card):
            skipped_tagged += 1
            continue
        tagless.append(card)

    if skipped_tagged:
        log(f"Skipping {skipped_tagged} saved '{target_tag}' candidate card(s) that now have tags.")
    if skipped_missing:
        log(f"Skipping {skipped_missing} saved '{target_tag}' candidate card(s) that could not be found for current-state validation.")
    return tagless


def verify_batch_tags_removed(page: Page, batch: Sequence[CardRecord], target_tag: str, delay_ms: int) -> None:
    """Search each card after removal and confirm the target tag is gone."""

    still_tagged: list[str] = []
    for card in batch:
        status = card_tag_status_by_search(page, card, target_tag, delay_ms)
        if status is True:
            still_tagged.append(card.title)
        elif status is None:
            log(f"Could not find '{card.title}' during post-removal search verification; relying on removed detail chip.")

    errors = []
    if still_tagged:
        errors.append(f"still tagged: {', '.join(still_tagged)}")
    if errors:
        raise RuntimeError("Tag removal verification failed; " + "; ".join(errors))


def read_card_detail_text(page: Page) -> str:
    """Return text from the open card detail dialog, excluding the dimmed collection."""

    try:
        return str(
            page.evaluate(
                """
                () => {
                  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                  const isVisible = (element, rect) => {
                    const style = window.getComputedStyle(element);
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                  };
                  const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')]
                    .map((element) => {
                      const rect = element.getBoundingClientRect();
                      if (!isVisible(element, rect)) return null;
                      if (rect.width < 360 || rect.height < 320) return null;
                      const text = normalize(element.innerText || '');
                      if (!/mettre aux encheres|mettre aux enchères|q-score/.test(text.toLowerCase())) {
                        return null;
                      }
                      return { text, area: rect.width * rect.height };
                    })
                    .filter(Boolean)
                    .sort((a, b) => a.area - b.area);
                  return dialogs[0]?.text || '';
                }
                """
            )
        )
    except PlaywrightError:
        return ""


def card_detail_matches(page: Page, card: CardRecord) -> bool:
    """Confirm the open detail modal belongs to the card we intend to mutate."""

    detail_text = normalize_text(read_card_detail_text(page))
    if not detail_text:
        return False
    if normalize_text(card.title) not in detail_text:
        return False
    if card.subtitle and normalize_text(card.subtitle) not in detail_text:
        return False
    return True


def close_card_detail(page: Page) -> None:
    """Close an open card detail modal without touching card data."""

    close_button = get_first_visible(
        [
            page.get_by_role("button", name=re.compile("^fermer$|^close$", re.IGNORECASE)),
            page.get_by_text(re.compile("^fermer$|^close$", re.IGNORECASE)),
        ],
        timeout_ms=800,
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


def open_card_detail_with_fallback(page: Page, card: CardRecord, delay_ms: int) -> None:
    """Open one exact card detail, checking the modal title before any mutation."""

    candidate_pages = nearby_page_numbers(card.page_number, card.page_total) or (card.page_number,)
    for page_number in candidate_pages:
        if page_number is None:
            continue
        page.goto(COLLECTION_URL, wait_until="domcontentloaded")
        settle_page(page)
        go_to_collection_page(page, page_number)
        clear_collection_filter(page, delay_ms)
        point = find_card_click_point_by_scrolling(page, card, delay_ms, selection_mode=False)
        if point is None:
            continue
        page.mouse.click(point["x"], point["y"])
        ui_pause(page, 1_000)
        if card_detail_matches(page, card):
            return
        close_card_detail(page)

    page.goto(COLLECTION_URL, wait_until="domcontentloaded")
    settle_page(page)
    filter_collection(page, card.title, delay_ms)
    ui_pause(page, 1_000)
    point = find_visible_card_click_point(page, card, selection_mode=False)
    if point is not None:
        page.mouse.click(point["x"], point["y"])
        ui_pause(page, 1_000)
        if card_detail_matches(page, card):
            return
        close_card_detail(page)

    raise RuntimeError(f"Could not open exact card detail for tag removal: {card.title}")


def remove_tag_from_card_detail(page: Page, card: CardRecord, target_tag: str, delay_ms: int) -> None:
    """Remove an existing tag via the card detail chip's X button."""

    open_card_detail_with_fallback(page, card, delay_ms)
    remove_pattern = re.compile(
        rf"retirer.*[ée]tiquette.*{re.escape(target_tag)}|retirer.*etiquette.*{re.escape(target_tag)}",
        re.IGNORECASE,
    )
    remove_button = get_first_visible(
        [
            page.get_by_role("button", name=remove_pattern),
            page.get_by_text(remove_pattern),
        ],
        timeout_ms=1_500,
    )
    if remove_button is None:
        close_card_detail(page)
        raise RuntimeError(f"Could not find remove button for tag '{target_tag}' on {card.title}.")

    remove_button.click(timeout=3_000)
    ui_pause(page, delay_ms)
    for _ in range(5):
        still_present = get_first_visible([page.get_by_role("button", name=remove_pattern)], timeout_ms=300)
        if still_present is None:
            close_card_detail(page)
            return
        ui_pause(page, delay_ms)

    close_card_detail(page)
    raise RuntimeError(f"Tag chip '{target_tag}' was still visible after removal click on {card.title}.")


def apply_single_card_by_search(
    page: Page,
    card: CardRecord,
    target_tag: str,
    selection_delay_ms: int,
    batch_delay_ms: int,
    confirmed_tags_path: Path = DEFAULT_CONFIRMED_TAGS_PATH,
) -> None:
    """Apply one tag via search first, then nearby pages if search misses."""

    page.goto(COLLECTION_URL, wait_until="domcontentloaded")
    settle_page(page)
    click_select_mode(page)
    clear_collection_filter(page, selection_delay_ms)
    try:
        select_card(page, card, selection_delay_ms, force_search=True)
    except RuntimeError as search_error:
        log(f"Search fallback could not find '{card.title}'; retrying recorded/nearby pages.")
        page.goto(COLLECTION_URL, wait_until="domcontentloaded")
        settle_page(page)
        go_to_collection_page(page, card.page_number)
        click_select_mode(page)
        clear_collection_filter(page, selection_delay_ms)
        try:
            select_card_with_page_fallback(page, card, selection_delay_ms)
        except RuntimeError as page_error:
            raise RuntimeError(f"{search_error}; page fallback also failed: {page_error}") from page_error

    selected_enough, actual_selected_count = selected_count_is_at_least(page, 1, timeout_ms=1_000)
    if not selected_enough:
        count_hint = "unknown" if actual_selected_count is None else str(actual_selected_count)
        raise SelectionLostError(f"Expected 1 selected card before tagging '{target_tag}', but the UI shows {count_hint}.")

    ui_pause(page, batch_delay_ms)
    bulk_result = add_tag_to_selected(page, target_tag, expected_count=1)
    ui_pause(page, batch_delay_ms)
    if bulk_result is None or bulk_result.successful_count < 1:
        verify_batch_tags(page, [card], target_tag, selection_delay_ms)
    record_confirmed_tags(target_tag, [card], confirmed_tags_path)


def iter_page_batches(
    cards: Sequence[CardRecord],
    batch_size: int,
    descending: bool = False,
) -> Iterable[tuple[int | None, list[CardRecord]]]:
    """Yield mutation batches that never span collection pages."""

    cards_by_page: dict[int | None, list[CardRecord]] = {}
    for card in cards:
        cards_by_page.setdefault(card.page_number, []).append(card)

    def page_sort_key(page_number: int | None) -> int:
        return page_number if page_number is not None else 1_000_000

    for page_number in sorted(cards_by_page, key=page_sort_key, reverse=descending):
        page_cards = cards_by_page[page_number]
        for start in range(0, len(page_cards), batch_size):
            yield page_number, list(page_cards[start : start + batch_size])


def apply_tag_batches(
    page: Page,
    cards: Sequence[CardRecord],
    target_tag: str,
    batch_size: int,
    selection_delay_ms: int,
    batch_delay_ms: int,
    confirmed_tags_path: Path = DEFAULT_CONFIRMED_TAGS_PATH,
) -> list[CardRecord]:
    """Apply one target tag to candidates in small, verified UI batches."""

    validate_apply_candidates_for_tag(cards, target_tag)
    applied: list[CardRecord] = []
    batches = list(iter_page_batches(cards, batch_size, descending=True))
    for batch_index, (page_number, batch) in enumerate(batches, start=1):
        page.goto(COLLECTION_URL, wait_until="domcontentloaded")
        settle_page(page)
        go_to_collection_page(page, page_number)
        click_select_mode(page)
        clear_collection_filter(page, selection_delay_ms)

        page_hint = f" on page {page_number}" if page_number else ""
        log(f"Selecting {len(batch)} card(s){page_hint} for batch {batch_index}/{len(batches)}.")
        selected_count = 0
        retry_individually = False
        retry_reason = ""
        for card in batch:
            try:
                selected_page, used_filter = select_card_with_page_fallback(page, card, selection_delay_ms)
            except RuntimeError as exc:
                if selected_count > 0 and len(batch) > 1:
                    retry_individually = True
                    retry_reason = str(exc)
                    break
                raise
            if len(batch) > 1 and used_filter:
                retry_individually = True
                retry_reason = f"'{card.title}' required collection search, which resets multi-selection."
                break
            if len(batch) > 1 and selected_page != page_number:
                retry_individually = True
                retry_reason = f"'{card.title}' moved from recorded page {page_number} to page {selected_page}."
                break
            selected_count += 1

        if retry_individually:
            log(
                f"Batch {batch_index}/{len(batches)} cannot stay on one page ({retry_reason}) "
                "Retrying its cards one by one via search."
            )
            for card in batch:
                apply_single_card_by_search(
                    page,
                    card,
                    target_tag,
                    selection_delay_ms,
                    batch_delay_ms,
                    confirmed_tags_path,
                )
                applied.append(card)
                log(f"Applied '{target_tag}' to {len(applied)}/{len(cards)} candidate card(s).")
            continue

        selected_enough, actual_selected_count = selected_count_is_at_least(page, len(batch), timeout_ms=1_000)
        if not selected_enough:
            if len(batch) == 1:
                count_hint = "unknown" if actual_selected_count is None else str(actual_selected_count)
                log(
                    f"Selection count for '{batch[0].title}' dropped to {count_hint}; "
                    "retrying the card via search."
                )
                apply_single_card_by_search(
                    page,
                    batch[0],
                    target_tag,
                    selection_delay_ms,
                    batch_delay_ms,
                    confirmed_tags_path,
                )
                applied.append(batch[0])
                log(f"Applied '{target_tag}' to {len(applied)}/{len(cards)} candidate card(s).")
                continue
            count_hint = "unknown" if actual_selected_count is None else str(actual_selected_count)
            raise SelectionLostError(
                f"Expected at least {len(batch)} selected card(s) before tagging '{target_tag}', "
                f"but the UI shows {count_hint}."
            )

        ui_pause(page, batch_delay_ms)
        try:
            bulk_result = add_tag_to_selected(page, target_tag, expected_count=len(batch))
        except SelectionLostError as exc:
            if len(batch) != 1:
                raise
            log(f"Selection was lost before bulk tagging '{batch[0].title}': {exc} Retrying via search.")
            apply_single_card_by_search(
                page,
                batch[0],
                target_tag,
                selection_delay_ms,
                batch_delay_ms,
                confirmed_tags_path,
            )
            applied.append(batch[0])
            log(f"Applied '{target_tag}' to {len(applied)}/{len(cards)} candidate card(s).")
            continue
        ui_pause(page, batch_delay_ms)
        if bulk_result is None or bulk_result.successful_count < len(batch):
            verify_batch_tags(page, batch, target_tag, selection_delay_ms)
        record_confirmed_tags(target_tag, batch, confirmed_tags_path)
        applied.extend(batch)
        log(f"Applied '{target_tag}' to {len(applied)}/{len(cards)} candidate card(s).")

    return applied


def remove_tag_batches(
    page: Page,
    cards: Sequence[CardRecord],
    target_tag: str,
    batch_size: int,
    selection_delay_ms: int,
    batch_delay_ms: int,
    confirmed_tags_path: Path = DEFAULT_CONFIRMED_TAGS_PATH,
) -> list[CardRecord]:
    """Remove one target tag from already-tagged cards via verified detail chips."""

    removed: list[CardRecord] = []
    batches = list(iter_page_batches(cards, batch_size))
    for batch_index, (page_number, batch) in enumerate(batches, start=1):
        page_hint = f" on page {page_number}" if page_number else ""
        log(f"Removing '{target_tag}' from {len(batch)} card(s){page_hint} for removal batch {batch_index}/{len(batches)}.")
        for card in batch:
            remove_tag_from_card_detail(page, card, target_tag, selection_delay_ms)
            verify_batch_tags_removed(page, [card], target_tag, selection_delay_ms)
            forget_confirmed_tags(target_tag, [card], confirmed_tags_path)
            removed.append(card)
            log(f"Removed '{target_tag}' from {len(removed)}/{len(cards)} invalid existing card(s).")
            ui_pause(page, batch_delay_ms)

    return removed


def write_report(
    path: Path,
    cards: Sequence[CardRecord],
    classifications: dict[str, dict[str, TagClassification]],
    tags: Sequence[str],
    dry_run: bool,
    applied: dict[str, Sequence[CardRecord]] | None = None,
    invalid_existing: dict[str, Sequence[CardRecord]] | None = None,
    removed: dict[str, Sequence[CardRecord]] | None = None,
    sample_per_tag: int = 10,
    write_summary: bool = True,
) -> None:
    """Write a grouped Markdown report for dry-run review or apply results."""

    path.parent.mkdir(parents=True, exist_ok=True)
    applied = applied or {}
    invalid_existing = invalid_existing or {}
    removed = removed or {}
    already_tagged_by_tag: dict[str, list[CardRecord]] = {}
    applied_keys_by_tag = {tag: {card.key for card in applied_cards} for tag, applied_cards in applied.items()}
    removed_keys_by_tag = {tag: {card.key for card in removed_cards} for tag, removed_cards in removed.items()}
    candidates_by_tag = build_candidates_by_tag(cards, classifications, tags)
    for tag in tags:
        already_tagged_by_tag[tag] = [card for card in cards if has_target_tag(card, tag)]

    total_candidates = sum(len(cards_for_tag) for cards_for_tag in candidates_by_tag.values())
    total_applied = sum(len(cards_for_tag) for cards_for_tag in applied.values())
    total_invalid_existing = sum(len(cards_for_tag) for cards_for_tag in invalid_existing.values())
    total_removed = sum(len(cards_for_tag) for cards_for_tag in removed.values())
    sample_label = "all" if sample_per_tag <= 0 else str(sample_per_tag)

    lines = [
        "# WikiMasters Topic Tag Report",
        "",
        f"- Timestamp: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Mode: {'dry-run' if dry_run else 'apply'}",
        f"- Enabled tags: {', '.join(f'`{tag}`' for tag in tags)}",
        f"- Sample per tag in report: {sample_label}",
        f"- Cards scanned: {len(cards)}",
        f"- Candidate card/tag additions: {total_candidates}",
        f"- Card/tag additions applied this run: {total_applied}",
        f"- Invalid existing card/tag assignments: {total_invalid_existing}",
        f"- Invalid existing card/tag assignments removed this run: {total_removed}",
        "",
        "| Tag | Already tagged | Candidates needing tag | Applied this run | Invalid existing | Removed invalid |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    for tag in tags:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(tag),
                    markdown_cell(len(already_tagged_by_tag[tag])),
                    markdown_cell(len(candidates_by_tag[tag])),
                    markdown_cell(len(applied.get(tag, ()))),
                    markdown_cell(len(invalid_existing.get(tag, ()))),
                    markdown_cell(len(removed.get(tag, ()))),
                ]
            )
            + " |"
        )

    if not total_candidates and not total_invalid_existing:
        lines.extend(["", "No untagged candidate cards or invalid existing tags were found for the enabled tags."])

    for tag in tags:
        definition = TAG_DEFINITIONS[tag]
        candidates = candidates_by_tag[tag]
        lines.extend(["", f"## `{tag}`", "", definition.description, ""])
        if not candidates:
            lines.append("No untagged candidates found.")
        else:
            shown_candidates = candidates[:sample_per_tag] if sample_per_tag > 0 else candidates
            hidden_count = len(candidates) - len(shown_candidates)
            lines.extend(
                [
                    "| Title | Subtitle | Page | Rarity | Tags | Score | Reason | Action |",
                    "|---|---|---:|---:|---|---:|---|---|",
                ]
            )
            for card in shown_candidates:
                classification = classifications[tag][card.key]
                action = "applied" if card.key in applied_keys_by_tag.get(tag, set()) else ("would apply" if dry_run else "pending")
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            markdown_cell(card.title),
                            markdown_cell(card.subtitle),
                            markdown_cell(card.page_number or ""),
                            markdown_cell(card.rarity),
                            markdown_cell(", ".join(card.tags)),
                            markdown_cell(classification.score),
                            markdown_cell(classification.reason),
                            markdown_cell(action),
                        ]
                    )
                    + " |"
                )
            if hidden_count > 0:
                lines.append("")
                lines.append(f"{hidden_count} additional `{tag}` candidate(s) omitted by `--sample-per-tag`.")

        invalid_cards = list(invalid_existing.get(tag, ()))
        lines.extend(["", f"### Invalid Existing `{tag}` Tags", ""])
        if not invalid_cards:
            lines.append("No invalid existing tags found.")
            continue

        shown_invalid = invalid_cards[:sample_per_tag] if sample_per_tag > 0 else invalid_cards
        hidden_invalid_count = len(invalid_cards) - len(shown_invalid)
        lines.extend(
            [
                "| Title | Subtitle | Page | Rarity | Tags | Score | Reason | Action |",
                "|---|---|---:|---:|---|---:|---|---|",
            ]
        )
        for card in shown_invalid:
            classification = classifications[tag][card.key]
            if card.key in removed_keys_by_tag.get(tag, set()):
                action = "removed"
            else:
                action = "would remove" if dry_run else "pending removal"
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(card.title),
                        markdown_cell(card.subtitle),
                        markdown_cell(card.page_number or ""),
                        markdown_cell(card.rarity),
                        markdown_cell(", ".join(card.tags)),
                        markdown_cell(classification.score),
                        markdown_cell(classification.reason),
                        markdown_cell(action),
                    ]
                )
                + " |"
            )
        if hidden_invalid_count > 0:
            lines.append("")
            lines.append(f"{hidden_invalid_count} additional invalid existing `{tag}` tag(s) omitted by `--sample-per-tag`.")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if write_summary:
        write_github_tagged_summary(applied, tags)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI used for local dry runs and optional apply runs."""

    parser = argparse.ArgumentParser(description="Tag WikiMasters collection cards for supported topics.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report candidates without changing WikiMasters tags.")
    mode.add_argument("--apply", action="store_true", help="Apply matching tags through the WikiMasters bulk UI.")
    mode.add_argument(
        "--apply-candidates",
        action="store_true",
        help="Apply tags from the saved candidate JSON without rescanning the full collection.",
    )
    mode.add_argument(
        "--remove-invalid-existing",
        action="store_true",
        help="Remove enabled tags from already-tagged cards that no longer match the classifier.",
    )
    parser.add_argument(
        "--tags",
        default=None,
        help=f"Comma-separated tags to classify. Defaults to all supported tags: {DEFAULT_TAGS}.",
    )
    parser.add_argument(
        "--exclude-tags",
        default=None,
        help="Comma-separated supported tags to exclude from the enabled tag set.",
    )
    parser.add_argument("--target-tag", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--audit-existing",
        action="store_true",
        help="Also classify already-tagged cards and report existing tags that should be removed.",
    )
    parser.add_argument("--sample-per-tag", type=int, default=10, help="Maximum candidates shown per tag in the report. 0 shows all.")
    parser.add_argument("--max-cards", type=int, default=0, help="Maximum cards to scan. 0 means all loaded cards.")
    parser.add_argument("--batch-size", type=int, default=8, help="Cards to select and tag per mutation batch.")
    parser.add_argument(
        "--max-apply-candidates",
        type=int,
        default=0,
        help="Maximum candidates to mutate per tag during apply. 0 means all candidates.",
    )
    parser.add_argument("--scroll-delay-ms", type=int, default=700, help="Delay after each collection scroll.")
    parser.add_argument("--selection-delay-ms", type=int, default=250, help="Delay between selection/search actions.")
    parser.add_argument("--batch-delay-ms", type=int, default=1_500, help="Delay before and after bulk tag application.")
    parser.add_argument(
        "--jitter-ms",
        type=int,
        default=DEFAULT_UI_JITTER_MS,
        help="Maximum tiny random UI pause added after scripted actions. 0 disables jitter.",
    )
    parser.add_argument("--wikipedia-delay-ms", type=int, default=500, help="Delay between Wikipedia API batches.")
    parser.add_argument("--wikipedia-batch-size", type=int, default=20, help="Wikipedia titles per API request.")
    parser.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH, help="Wikipedia metadata cache path.")
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH, help="Markdown report output path.")
    parser.add_argument("--candidate-path", type=Path, default=DEFAULT_CANDIDATE_PATH, help="JSON candidate artifact path.")
    parser.add_argument(
        "--confirmed-tags-path",
        type=Path,
        default=DEFAULT_CONFIRMED_TAGS_PATH,
        help="Local cache of card/tag pairs confirmed by previous WikiMasters bulk results.",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the full scan, classify, report, and optional apply workflow."""

    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Run `python -m pip install -r requirements.txt` first.")

    dry_run = not args.apply and not args.apply_candidates and not args.remove_invalid_existing
    audit_existing = args.audit_existing or args.remove_invalid_existing
    email = required_env("WIKIMASTERS_EMAIL")
    password = required_env("WIKIMASTERS_PASSWORD")
    headless = os.environ.get("HEADLESS", "1").lower() not in {"0", "false", "no"}
    if args.remove_invalid_existing and not (args.tags or args.target_tag):
        raise RuntimeError("--remove-invalid-existing requires an explicit --tags value.")
    try:
        enabled_tags = resolve_enabled_tags(args.tags, args.target_tag, args.exclude_tags)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if args.batch_size < 1:
        raise RuntimeError("--batch-size must be at least 1.")
    if args.max_apply_candidates < 0:
        raise RuntimeError("--max-apply-candidates cannot be negative.")
    if args.jitter_ms < 0:
        raise RuntimeError("--jitter-ms cannot be negative.")
    if args.sample_per_tag < 0:
        raise RuntimeError("--sample-per-tag cannot be negative.")
    set_ui_jitter_ms(args.jitter_ms)

    page: Page | None = None
    cards: list[CardRecord] = []
    classifications: dict[str, dict[str, TagClassification]] = {}
    applied: dict[str, list[CardRecord]] = {tag: [] for tag in enabled_tags}
    invalid_existing: dict[str, list[CardRecord]] = {tag: [] for tag in enabled_tags}
    removed: dict[str, list[CardRecord]] = {tag: [] for tag in enabled_tags}

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

            if args.apply_candidates:
                requested_tags = enabled_tags or None
                candidate_artifact = load_candidate_artifact(args.candidate_path, requested_tags=requested_tags)
                enabled_tags = candidate_artifact.tags
                applied = {tag: [] for tag in enabled_tags}
                log(
                    f"Loaded saved candidates from {args.candidate_path} "
                    f"({candidate_artifact.cards_scanned} cards scanned at {candidate_artifact.generated_at or 'unknown time'})."
                )
                already_applied_card_keys: set[str] = set()
                for tag in enabled_tags:
                    saved_candidates = candidate_artifact.cards_by_tag[tag]
                    skipped_tagged = [card for card in saved_candidates if has_any_tag(card)]
                    candidates = [
                        card
                        for card in saved_candidates
                        if not has_any_tag(card) and card.key not in already_applied_card_keys
                    ]
                    skipped_already_applied_count = len(saved_candidates) - len(skipped_tagged) - len(candidates)
                    if skipped_tagged:
                        log(f"Skipping {len(skipped_tagged)} saved '{tag}' candidate card(s) that already have tags.")
                    if skipped_already_applied_count:
                        log(f"Skipping {skipped_already_applied_count} saved '{tag}' candidate card(s) already tagged earlier in this run.")
                    candidates = filter_current_tagless_candidates(page, candidates, tag, args.selection_delay_ms)
                    if args.max_apply_candidates:
                        candidates = candidates[: args.max_apply_candidates]
                    if not candidates:
                        log(f"No saved '{tag}' candidates to apply.")
                        continue
                    log(f"Applying '{tag}' to {len(candidates)} saved candidate card(s).")
                    applied[tag] = apply_tag_batches(
                        page,
                        candidates,
                        tag,
                        args.batch_size,
                        args.selection_delay_ms,
                        args.batch_delay_ms,
                        args.confirmed_tags_path,
                    )
                    already_applied_card_keys.update(card.key for card in applied[tag])
                total_applied = sum(len(tag_cards) for tag_cards in applied.values())
                write_github_tagged_summary(applied, enabled_tags)
                log(f"Run completed successfully. Applied {total_applied} saved candidate card/tag addition(s).")
                return 0

            cards = scan_collection(page, args.max_cards, args.scroll_delay_ms)
            confirmed_tags = load_confirmed_tag_keys(args.confirmed_tags_path)
            cards, confirmed_count = apply_confirmed_tags(cards, confirmed_tags, enabled_tags)
            if confirmed_count:
                log(f"Applied {confirmed_count} locally confirmed card/tag pair(s) to the scanned grid data.")
            cards_needing_any_enabled_tag_check = [
                card
                for card in cards
                if not has_any_tag(card) or (audit_existing and any(has_target_tag(card, tag) for tag in enabled_tags))
            ]
            log(
                f"Found {len(cards)} scanned card(s); {len(cards_needing_any_enabled_tag_check)} "
                "need at least one enabled tag check."
            )

            wikipedia = WikipediaClient(args.cache_path, args.wikipedia_delay_ms, args.wikipedia_batch_size)
            metadata_by_title = wikipedia.fetch_many([card.title for card in cards_needing_any_enabled_tag_check])
            tagless_cards = [card for card in cards if not has_any_tag(card)]
            for tag in enabled_tags:
                tagged_cards = [card for card in cards if has_target_tag(card, tag)]
                cards_to_classify = tagless_cards + (tagged_cards if audit_existing else [])
                classifications[tag] = {
                    card.key: classify_card_for_tag(tag, card, metadata_by_title.get(card.title))
                    for card in cards_to_classify
                }
                invalid_existing[tag] = [
                    card for card in tagged_cards if not classifications.get(tag, {}).get(card.key, TagClassification(tag, False, 0, "")).is_match
                ] if audit_existing else []

            candidates_by_tag = build_candidates_by_tag(cards, classifications, enabled_tags)
            for tag in enabled_tags:
                tagged_cards = [card for card in cards if has_target_tag(card, tag)]
                log(
                    f"Found {len(candidates_by_tag[tag])} untagged '{tag}' candidate card(s); "
                    f"{len(tagged_cards)} already tagged."
                )
                if audit_existing:
                    log(f"Found {len(invalid_existing[tag])} invalid existing '{tag}' tag(s).")

            total_candidates = sum(len(tag_candidates) for tag_candidates in candidates_by_tag.values())
            write_candidate_artifact(args.candidate_path, cards, classifications, enabled_tags)
            log(f"Wrote reusable candidates to {args.candidate_path}.")
            write_report(
                args.report_path,
                cards,
                classifications,
                enabled_tags,
                dry_run=dry_run,
                invalid_existing=invalid_existing if audit_existing else None,
                sample_per_tag=args.sample_per_tag,
                write_summary=dry_run or (not total_candidates and not args.remove_invalid_existing),
            )
            log(f"Wrote report to {args.report_path}.")

            if args.remove_invalid_existing:
                total_invalid_existing = sum(len(tag_cards) for tag_cards in invalid_existing.values())
                if not total_invalid_existing:
                    log("No invalid existing tags found to remove.")
                    return 0
                for tag in enabled_tags:
                    invalid_cards = invalid_existing[tag]
                    if args.max_apply_candidates:
                        invalid_cards = invalid_cards[: args.max_apply_candidates]
                    if not invalid_cards:
                        continue
                    log(f"Removing invalid existing '{tag}' tag from {len(invalid_cards)} card(s).")
                    removed[tag] = remove_tag_batches(
                        page,
                        invalid_cards,
                        tag,
                        args.batch_size,
                        args.selection_delay_ms,
                        args.batch_delay_ms,
                        args.confirmed_tags_path,
                    )

                write_report(
                    args.report_path,
                    cards,
                    classifications,
                    enabled_tags,
                    dry_run=False,
                    invalid_existing=invalid_existing,
                    removed=removed,
                    sample_per_tag=args.sample_per_tag,
                    write_summary=False,
                )
                total_removed = sum(len(tag_cards) for tag_cards in removed.values())
                log(f"Run completed successfully. Removed {total_removed} invalid existing card/tag assignment(s).")
                return 0

            if dry_run or not total_candidates:
                if dry_run:
                    log("Dry run completed; no WikiMasters tags were changed.")
                return 0

            for tag in enabled_tags:
                candidates = candidates_by_tag[tag]
                if args.max_apply_candidates:
                    candidates = candidates[: args.max_apply_candidates]
                if not candidates:
                    continue
                log(f"Applying '{tag}' to {len(candidates)} candidate card(s).")
                applied[tag] = apply_tag_batches(
                    page,
                    candidates,
                    tag,
                    args.batch_size,
                    args.selection_delay_ms,
                    args.batch_delay_ms,
                    args.confirmed_tags_path,
                )

            write_report(
                args.report_path,
                cards,
                classifications,
                enabled_tags,
                dry_run=False,
                applied=applied,
                invalid_existing=invalid_existing if audit_existing else None,
                sample_per_tag=args.sample_per_tag,
            )
            total_applied = sum(len(tag_cards) for tag_cards in applied.values())
            log(f"Run completed successfully. Applied {total_applied} card/tag addition(s).")
            return 0
        except Exception as exc:
            log(f"Run failed: {exc}")
            if cards and classifications:
                write_report(
                    args.report_path,
                    cards,
                    classifications,
                    enabled_tags,
                    dry_run=dry_run,
                    applied=applied,
                    invalid_existing=invalid_existing if audit_existing else None,
                    removed=removed,
                    sample_per_tag=args.sample_per_tag,
                )
            save_failure_artifacts(page, "wikimasters-tag-cards-failure", str(exc))
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
