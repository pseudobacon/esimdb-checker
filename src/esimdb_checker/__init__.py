"""esimdb_checker — research travel eSIM plans on esimdb.com and recommend a
whole-itinerary purchase strategy.

Research/recommend only: this package performs read-only GET requests against the
structured JSON endpoints the esimdb.com website uses, and prints recommendations.
It never logs in, buys, or activates anything.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .models import Country, Itinerary, Plan, Coverage, Recommendation, Report

__all__ = [
    "__version__",
    "Country",
    "Itinerary",
    "Plan",
    "Coverage",
    "Recommendation",
    "Report",
]
