"""The five step tabs, in display order.

Each tab lives in its own package (``tabs/<key>/``) and is owned by one thread; see
docs/ui-architecture.md. To add a tab, add its class here.
"""

from .air import AirTab
from .base import StepTab
from .model import ModelTab
from .results import ResultsTab
from .simulation import SimulationTab
from .volume import VolumeTab

TAB_CLASSES: list[type[StepTab]] = [ModelTab, AirTab, VolumeTab, SimulationTab, ResultsTab]

__all__ = ["TAB_CLASSES", "AirTab", "ModelTab", "ResultsTab", "SimulationTab", "StepTab",
           "VolumeTab"]
