"""Public statistics models, catalog metadata, and persistence services."""

from .catalog import CATALOG_ENTRIES, CATALOG_VERSION, find_catalog_entry
from .models import (
    CatalogEntry,
    CatchRecord,
    HistoricalStatsSnapshot,
    SessionStats,
    SpeciesStats,
    StatsSnapshot,
)
from .repository import SQLiteStatsRepository
from .service import StatsService

__all__ = [
    "CATALOG_ENTRIES",
    "CATALOG_VERSION",
    "CatalogEntry",
    "CatchRecord",
    "HistoricalStatsSnapshot",
    "SQLiteStatsRepository",
    "SessionStats",
    "SpeciesStats",
    "StatsService",
    "StatsSnapshot",
    "find_catalog_entry",
]
