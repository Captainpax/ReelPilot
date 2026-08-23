"""Versioned factual catalog used to enrich recognized catch results.

Only interoperability facts are stored here: vanilla identifiers, English names,
base sell prices, and fish motion metadata. ReelPilot does not redistribute game
descriptions, art, fonts, or XNB assets. Values target Stardew Valley 1.6.15.24356.
"""

from __future__ import annotations

from ..domain import ResultType
from .models import CatalogEntry

CATALOG_VERSION = "stardew-1.6.15.24356-v1"


def _fish(
    item_id: str,
    name: str,
    price_gold: int,
    difficulty: int,
    motion: str,
) -> CatalogEntry:
    """Build a fishing-rod entry with a qualified object identifier."""
    qualified_id = item_id if item_id.startswith("(O)") else f"(O){item_id}"
    return CatalogEntry(
        qualified_id,
        name,
        ResultType.FISH,
        price_gold,
        difficulty,
        motion,
        CATALOG_VERSION,
    )


def _item(item_id: str, name: str, price_gold: int) -> CatalogEntry:
    """Build a non-fish catch-result entry."""
    qualified_id = item_id if item_id.startswith("(O)") else f"(O){item_id}"
    return CatalogEntry(
        qualified_id,
        name,
        ResultType.ITEM,
        price_gold,
        None,
        None,
        CATALOG_VERSION,
    )


# Difficulty is the minigame motion score, not a universal spawn probability.
CATALOG_ENTRIES: tuple[CatalogEntry, ...] = (
    _fish("705", "Albacore", 75, 60, "mixed"),
    _fish("129", "Anchovy", 30, 30, "dart"),
    _fish("160", "Angler", 900, 85, "smooth"),
    _fish("800", "Blobfish", 500, 75, "floater"),
    _fish("838", "Blue Discus", 120, 60, "dart"),
    _fish("132", "Bream", 45, 35, "smooth"),
    _fish("700", "Bullhead", 75, 46, "smooth"),
    _fish("142", "Carp", 30, 15, "mixed"),
    _fish("143", "Catfish", 200, 75, "mixed"),
    _fish("702", "Chub", 50, 35, "dart"),
    _fish("159", "Crimsonfish", 1500, 95, "mixed"),
    _fish("704", "Dorado", 100, 78, "mixed"),
    _fish("148", "Eel", 85, 70, "smooth"),
    _fish("267", "Flounder", 100, 50, "sinker"),
    _fish("156", "Ghostfish", 45, 50, "mixed"),
    _fish("775", "Glacierfish", 1000, 100, "mixed"),
    _fish("Goby", "Goby", 150, 55, "dart"),
    _fish("708", "Halibut", 80, 50, "sinker"),
    _fish("147", "Herring", 30, 25, "dart"),
    _fish("161", "Ice Pip", 500, 85, "dart"),
    _fish("136", "Largemouth Bass", 100, 50, "mixed"),
    _fish("162", "Lava Eel", 700, 90, "mixed"),
    _fish("163", "Legend", 5000, 110, "mixed"),
    _fish("707", "Lingcod", 120, 85, "mixed"),
    _fish("837", "Lionfish", 100, 50, "smooth"),
    _fish("269", "Midnight Carp", 150, 55, "mixed"),
    _fish("798", "Midnight Squid", 100, 55, "sinker"),
    _fish("682", "Mutant Carp", 1000, 80, "dart"),
    _fish("149", "Octopus", 150, 95, "sinker"),
    _fish("141", "Perch", 55, 35, "mixed"),
    _fish("144", "Pike", 100, 60, "dart"),
    _fish("128", "Pufferfish", 200, 80, "floater"),
    _fish("138", "Rainbow Trout", 65, 45, "mixed"),
    _fish("146", "Red Mullet", 75, 55, "smooth"),
    _fish("150", "Red Snapper", 50, 40, "mixed"),
    _fish("139", "Salmon", 75, 50, "mixed"),
    _fish("164", "Sandfish", 75, 65, "mixed"),
    _fish("131", "Sardine", 40, 30, "dart"),
    _fish("165", "Scorpion Carp", 150, 90, "dart"),
    _fish("154", "Sea Cucumber", 75, 40, "sinker"),
    _fish("706", "Shad", 60, 45, "smooth"),
    _fish("796", "Slimejack", 100, 55, "dart"),
    _fish("137", "Smallmouth Bass", 50, 28, "mixed"),
    _fish("799", "Spook Fish", 220, 60, "dart"),
    _fish("151", "Squid", 80, 75, "sinker"),
    _fish("836", "Stingray", 180, 80, "sinker"),
    _fish("158", "Stonefish", 300, 65, "sinker"),
    _fish("698", "Sturgeon", 200, 78, "mixed"),
    _fish("145", "Sunfish", 30, 30, "mixed"),
    _fish("155", "Super Cucumber", 250, 80, "sinker"),
    _fish("699", "Tiger Trout", 150, 60, "dart"),
    _fish("701", "Tilapia", 75, 52, "mixed"),
    _fish("130", "Tuna", 100, 70, "smooth"),
    _fish("795", "Void Salmon", 150, 80, "mixed"),
    _fish("140", "Walleye", 105, 45, "smooth"),
    _fish("734", "Woodskip", 75, 50, "mixed"),
    _item("685", "Bait", 1),
    _item("171", "Broken CD", 0),
    _item("170", "Broken Glasses", 0),
    _item("382", "Coal", 15),
    _item("169", "Driftwood", 0),
    _item("153", "Green Algae", 15),
    _item("167", "Joja Cola", 25),
    _item("152", "Seaweed", 20),
    _item("172", "Soggy Newspaper", 0),
    _item("390", "Stone", 2),
    _item("168", "Trash", 0),
    _item("157", "White Algae", 25),
)

CATALOG_BY_NAME = {entry.canonical_name.casefold(): entry for entry in CATALOG_ENTRIES}
FISH_NAMES = tuple(
    entry.canonical_name for entry in CATALOG_ENTRIES if entry.result_type is ResultType.FISH
)
ITEM_NAMES = tuple(
    entry.canonical_name for entry in CATALOG_ENTRIES if entry.result_type is ResultType.ITEM
)


def find_catalog_entry(name: str | None) -> CatalogEntry | None:
    """Return a catalog entry by conservative English name matching."""
    return CATALOG_BY_NAME.get(name.casefold()) if name else None
