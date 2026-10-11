"""Meesho product discovery + detail extraction via Playwright.

Design notes
------------
* Meesho is a Next.js app, so product data is often present in the server
  HTML (SSR) and in ``script#__NEXT_DATA__``. We try DOM selectors first and
  fall back to JSON walking, so minor markup changes degrade gracefully.
* Selectors are intentionally defensive: several candidates per field.
* All extraction functions return ``None`` on failure instead of raising, so
  one bad product never kills the whole batch.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, Browser, Page

BASE = "https://www.meesho.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

QUERIES = [
    q.strip()
    for q in os.getenv(
        "MEESHO_QUERIES", "women kurti,mobile cover,kitchen organizer"
    ).split(",")
    if q.strip()
]

PRICE_RE = re.compile(r"₹\s*([\d,]+)")
REVIEWS_RE = re.compile(r"([\d,]+)\s*reviews?", re.I)
RATING_RE = re.compile(r"(\d(?:\.\d)?)\s*(?:/\s*5|out of 5|★)?")

AFFILIATE_TEMPLATE = os.getenv("MEESHO_AFFILIATE_TEMPLATE", "")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _num(text: str) -> Optional[int]:
    m = PRICE_RE.search(text) or REVIEWS_RE.search(text)
    if not m:
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits else None
    return int(m.group(1).replace(",", ""))


async def _text(page: Page, selectors: list[str], timeout: int = 4000) -> Optional[str]:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                txt = (await loc.inner_text(timeout=timeout)).strip()
                if txt:
                    return txt
        except Exception:
            continue
    return None


def _walk_json(node: Any, pred, depth: int = 0):
    """Depth-first search through decoded JSON for a matching value."""
    if depth > 12 or node is None:
        return None
    if isinstance(node, dict):
        hit = pred(node)
        if hit is not None:
            return hit
        for v in node.values():
            found = _walk_json(v, pred, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node[:50]:
            found = _walk_json(v, pred, depth + 1)
            if found is not None:
                return found
    return None


async def _next_data(page: Page) -> Optional[Any]:
    for sel in ("script#__NEXT_DATA__", "script[type='application/json']"):
        try:
            raw = await page.locator(sel).first.inner_text(timeout=2000)
            if raw and len(raw) > 50:
                return json.loads(raw)
        except Exception:
            continue
    return None


def _dist_from_json(data: Any) -> Optional[dict[str, float]]:
    """Find a 1..5 star distribution inside decoded page JSON."""

    def pred(d: dict):
        keys = {str(k).lower().replace("_", "").replace(" ", "") for k in d}
        # explicit distribution containers
        interesting = (
            "ratingdistribution", "ratingbreakdown", "starratingdistribution",
            "ratingstats", "ratingsummary", "distribution",
        )
        if keys & interesting or any(k.endswith("distribution") and "rating" in k for k in keys):
            for k, v in d.items():
                kl = str(k).lower().replace("_", "").replace(" ", "")
                if kl in interesting or (kl.endswith("distribution") and "rating" in kl):
                    norm = _normalize_dist(v)
                    if norm:
                        return norm
        # dict whose keys are star numbers
        star_keys = {str(k).strip().lower() for k in d}
        if star_keys <= {"1", "2", "3", "4", "5"} and len(star_keys) >= 3:
            norm = _normalize_dist(d)
            if norm:
                return norm
        # list of {star/count}
        return None

    return _walk_json(data, pred)


def _normalize_dist(v: Any) -> Optional[dict[str, float]]:
    out: dict[str, float] = {}
    if isinstance(v, dict):
        for k, val in v.items():
            star = str(k).strip().lower().replace("star", "").strip()
            if star in {"1", "2", "3", "4", "5"}:
                try:
                    if isinstance(val, dict):
                        val = val.get("count") or val.get("value") or val.get("percentage")
                    out[star] = float(val)
                except (TypeError, ValueError):
                    return None
    elif isinstance(v, list):
        for item in v:
            if not isinstance(item, dict):
                continue
            star = str(
                item.get("star") or item.get("rating") or item.get("value") or ""
            ).strip()
            star = star.replace("star", "").strip()
            if star in {"1", "2", "3", "4", "5"}:
                try:
                    out[star] = float(item.get("count") or item.get("votes") or item.get("percentage") or 0)
                except (TypeError, ValueError):
                    return None
    if len(out) >= 3:
        return out
    return None


def _dist_from_dom(text_blocks: list[str]) -> Optional[dict[str, float]]:
    """Parse lines like '5 Star 70%' / '4 star: 20' from rendered text."""
    out: dict[str, float] = {}
    for t in text_blocks:
        m = re.search(r"([1-5])\s*star\D{0,10}(\d+(?:\.\d+)?)\s*%?", t, re.I)
        if m:
            out[m.group(1)] = float(m.group(2))
    if len(out) >= 3:
        return out
    return None


def _affiliate_url(product_url: str, product_id: str, slug: str) -> str:
    if AFFILIATE_TEMPLATE:
        return AFFILIATE_TEMPLATE.format(id=product_id, slug=slug, url=product_url)
    return product_url


# --------------------------------------------------------------------------- #
# page-level extraction
# --------------------------------------------------------------------------- #
async def extract_product(page: Page, url: str) -> Optional[dict[str, Any]]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)
    except Exception:
        return None

    # product id from URL: /slug/p/123456789
    m = re.search(r"/p/(\d+)", urlparse(url).path)
    product_id = m.group(1) if m else url.rstrip("/").split("/")[-1]
    slug = urlparse(url).path.split("/p/")[0].strip("/")

    title = await _text(page, ["h1", "[data-testid='product-title']", "title"])
    if not title:
        return None
    title = title.replace("| Meesho", "").strip()

    try:
        body_text = await page.inner_text("body", timeout=5000)
    except Exception:
        body_text = ""

    price_txt = await _text(
        page,
        [
            "[data-testid='product-price']",
            "span[dir='auto']",
            "div:has-text('₹')",
        ],
    )
    price = _num(price_txt or "") or _num(body_text)

    rating_txt = await _text(
        page,
        [
            "[data-testid='product-rating']",
            "div:has-text('out of 5')",
            "span:has-text('★')",
        ],
    )
    rating = None
    if rating_txt:
        m = re.search(r"(\d\.\d)", rating_txt)
        if m:
            rating = float(m.group(1))
    if rating is None:
        m = re.search(r"(\d\.\d)\s*(?:/\s*5|out of 5)", body_text)
        if m:
            rating = float(m.group(1))

    reviews_count = None
    rev_txt = await _text(page, ["[data-testid='reviews-count']", "div:has-text('Reviews')"])
    if rev_txt:
        m = REVIEWS_RE.search(rev_txt)
        if m:
            reviews_count = int(m.group(1).replace(",", ""))
    if reviews_count is None:
        m = REVIEWS_RE.search(body_text)
        if m:
            reviews_count = int(m.group(1).replace(",", ""))

    # images
    images: list[str] = []
    try:
        srcs = await page.eval_on_selector_all(
            "img",
            "els => els.map(e => e.currentSrc || e.src).filter(Boolean)",
        )
        seen = set()
        for s in srcs:
            if "images.meesho.com" in s and s not in seen:
                seen.add(s)
                images.append(s.split("?")[0])
    except Exception:
        pass

    # features / highlights
    features: list[str] = []
    try:
        items = await page.eval_on_selector_all(
            "li",
            "els => els.map(e => (e.innerText||'').trim()).filter(t => t.length > 3 && t.length < 120)",
        )
        junk = re.compile(r"reviews?|rating|return|refund|delivery|cod|₹", re.I)
        for t in items[:20]:
            if not junk.search(t):
                features.append(t)
            if len(features) >= 6:
                break
    except Exception:
        pass

    # rating distribution: DOM text first, then embedded JSON
    dist: Optional[dict[str, float]] = None
    try:
        blocks = await page.eval_on_selector_all(
            "div,span,p",
            "els => els.map(e => (e.innerText||'').trim())"
            ".filter(t => t && t.length < 60 && /[1-5]\\s*star/i.test(t))",
        )
        dist = _dist_from_dom(blocks)
    except Exception:
        pass
    if dist is None:
        data = await _next_data(page)
        if data:
            dist = _dist_from_json(data)

    product = {
        "product_id": product_id,
        "title": title,
        "price": price,
        "images": images[:6],
        "rating": rating,
        "reviews_count": reviews_count,
        "rating_distribution": dist,
        "features": features,
        "url": url,
        "affiliate_url": _affiliate_url(url, product_id, slug),
    }
    product["return_risk_pct"] = (
        round(
            100.0
            * (
                float(dist.get("1", 0)) + float(dist.get("2", 0))
            )
            / max(sum(float(v) for v in dist.values()), 1e-9),
            2,
        )
        if dist
        else None
    )
    return product


async def discover_links(page: Page) -> list[str]:
    """Collect product links from the homepage and configured search queries."""
    links: list[str] = []
    seen: set[str] = set()

    def keep(href: str) -> bool:
        if not href or "/p/" not in href:
            return False
        full = urljoin(BASE, href.split("?")[0])
        if full in seen:
            return False
        seen.add(full)
        return True

    async def harvest(url: str) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)
            hrefs = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))"
            )
            for h in hrefs or []:
                if keep(h or ""):
                    links.append(urljoin(BASE, (h or "").split("?")[0]))
        except Exception:
            return

    await harvest(f"{BASE}/")
    if len(links) < 8:
        for q in QUERIES[:4]:
            await harvest(f"{BASE}/search?q={q.replace(' ', '%20')}")
            if len(links) >= 12:
                break
    return links


async def scrape_trending(limit: int = 5, strict: bool = True) -> dict[str, Any]:
    """Main entry: discover -> extract -> filter -> top-N."""
    from .filters import apply_filters

    results: dict[str, Any] = {
        "products": [],
        "count": 0,
        "scanned": 0,
        "rejected": [],
        "warnings": [],
    }

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = await ctx.new_page()
        try:
            links = await discover_links(page)
            if not links:
                results["warnings"].append("discovery_found_no_links")
                return results

            products: list[dict[str, Any]] = []
            # scan a bounded number of detail pages, stop early once we have
            # enough candidates to survive the filters
            for url in links[: max(limit * 6, 18)]:
                p = await extract_product(page, url)
                results["scanned"] += 1
                if p and p.get("title") and p.get("price"):
                    products.append(p)
                if len(products) >= limit * 4:
                    break

            accepted, rejected = apply_filters(products, limit=limit, strict_return_risk=strict)
            results["products"] = accepted
            results["count"] = len(accepted)
            results["rejected"] = rejected[:20]
            if not accepted and products:
                results["warnings"].append(
                    f"all_{len(products)}_candidates_filtered_out"
                )
            if not products:
                results["warnings"].append("no_product_pages_extracted")
        finally:
            await browser.close()

    return results
