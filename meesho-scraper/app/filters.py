"""Pure filtering logic for trending product selection.

Spec:
  - Rating >= 4.0
  - Reviews >= 200
  - Return-risk: 1-star + 2-star share of total reviews must be < 12%
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

MIN_RATING = 4.0
MIN_REVIEWS = 200
MAX_LOW_RATING_SHARE = 0.12  # 12%


def low_rating_share(product: dict[str, Any]) -> Optional[float]:
    """Return (1-star + 2-star share) in [0,1], or None when unavailable."""
    dist = product.get("rating_distribution") or {}
    # accepted shapes: {"1": 10, "2": 5, ...} counts, or {"1": 8.5, ...} percents
    try:
        one = float(dist.get("1") or dist.get("one") or 0)
        two = float(dist.get("2") or dist.get("two") or 0)
    except (TypeError, ValueError):
        return None
    total_stars = 0.0
    for key in ("1", "2", "3", "4", "5", "one", "two", "three", "four", "five"):
        try:
            total_stars += float(dist.get(key) or 0)
        except (TypeError, ValueError):
            continue
    if total_stars <= 0:
        # percentages were given without a usable total -> treat values as percents
        if 0 <= one <= 100 and 0 <= two <= 100:
            return (one + two) / 100.0
        return None
    return (one + two) / total_stars


def check_product(product: dict[str, Any], strict_return_risk: bool = True) -> tuple[bool, list[str]]:
    """Apply the three spec filters. Returns (passed, reasons)."""
    reasons: list[str] = []

    rating = product.get("rating")
    if rating is None or float(rating) < MIN_RATING:
        reasons.append(f"rating<{MIN_RATING}")

    reviews = product.get("reviews_count")
    if reviews is None or int(reviews) < MIN_REVIEWS:
        reasons.append(f"reviews<{MIN_REVIEWS}")

    share = low_rating_share(product)
    if share is None:
        if strict_return_risk:
            reasons.append("return_risk_unverifiable")
    elif share >= MAX_LOW_RATING_SHARE:
        reasons.append(f"low_rating_share>={MAX_LOW_RATING_SHARE:.0%}")

    return (len(reasons) == 0, reasons)


def relevance_score(product: dict[str, Any]) -> float:
    """Rank surviving products: rating dominant, review volume as tie-break."""
    rating = float(product.get("rating") or 0)
    reviews = int(product.get("reviews_count") or 0)
    return rating * 10 + math.log10(max(reviews, 1) * 10)


def apply_filters(
    products: Iterable[dict[str, Any]],
    limit: int = 5,
    strict_return_risk: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (accepted_sorted, rejected_with_reasons)."""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for p in products:
        ok, reasons = check_product(p, strict_return_risk=strict_return_risk)
        if ok:
            accepted.append(p)
        else:
            rejected.append({"product_id": p.get("product_id"), "reasons": reasons})
    accepted.sort(key=relevance_score, reverse=True)
    return accepted[:limit], rejected
