"""Meesho Scraper Service.

GET /trending?limit=5 -> spec-compliant JSON list of filtered products.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, HTTPException, Query

from .scraper import scrape_trending

app = FastAPI(title="meesho-scraper", version="1.0.0")

# one scrape at a time; the box is small
_lock = asyncio.Lock()
_last: dict | None = None

STRICT = os.getenv("RETURN_RISK_MODE", "strict").lower() != "lenient"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "strict_return_risk": STRICT}


@app.get("/trending")
async def trending(limit: int = Query(default=5, ge=1, le=20)) -> dict:
    """Return top `limit` products passing rating/reviews/return-risk filters."""
    global _last
    if _lock.locked() and _last is not None:
        # a scrape is already running; serve the freshest completed result
        return _last
    async with _lock:
        result = await scrape_trending(limit=limit, strict=STRICT)
        _last = result
        return result


@app.get("/cached")
async def cached() -> dict:
    """Last result without re-scraping (useful for n8n debugging)."""
    if _last is None:
        raise HTTPException(status_code=404, detail="no scrape has run yet")
    return _last
