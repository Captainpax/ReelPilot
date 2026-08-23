"""Screen capture and template-free fishing-scene detectors."""

from .energy import EnergyMeterDetector, FoodPromptDetector
from .pipeline import VisionPipeline
from .treasure import TreasureLootDetector

__all__ = [
    "EnergyMeterDetector",
    "FoodPromptDetector",
    "TreasureLootDetector",
    "VisionPipeline",
]
