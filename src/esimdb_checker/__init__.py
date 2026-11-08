"""esimdb_checker — research travel eSIM plans on esimdb.com and recommend a
complete purchase strategy for a per-leg travel itinerary.

Research/recommend only: this package performs read-only GET requests against the
structured JSON endpoints the esimdb.com website uses, and prints recommendations.
It never logs in, buys, or activates anything.
"""

from __future__ import annotations

__version__ = "0.2.0"

from .models import (
    Country,
    Fx,
    Leg,
    Plan,
    Report,
    Strategy,
    StrategyPlan,
    Trip,
)

__all__ = [
    "__version__",
    "Country",
    "Fx",
    "Leg",
    "Plan",
    "Report",
    "Strategy",
    "StrategyPlan",
    "Trip",
]
