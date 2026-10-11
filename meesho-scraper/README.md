# meesho-scraper

Python + Playwright FastAPI microservice that fetches trending / high-converting
Meesho products and applies the spec filters.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/trending?limit=5` | Discover + extract + filter, return top N |
| GET | `/cached` | Last result without re-scraping |
| GET | `/health` | Liveness |

## Output schema (per spec)

```json
{
  "product_id": "M12345",
  "title": "Product Title",
  "price": 499,
  "images": ["url1", "url2"],
  "rating": 4.2,
  "features": ["Feature 1", "Feature 2"],
  "affiliate_url": "https://..."
}
```

Extra fields returned: `reviews_count`, `rating_distribution`,
`return_risk_pct`, `url` — needed for audit/debugging.

## Filters

- rating >= 4.0
- reviews_count >= 200
- (1-star + 2-star share) < 12% of total reviews

`RETURN_RISK_MODE=strict` (default) excludes products whose star distribution
cannot be verified; `lenient` lets them through with `return_risk_pct: null`.

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `MEESHO_QUERIES` | `women kurti,mobile cover,kitchen organizer` | Search queries used for discovery |
| `RETURN_RISK_MODE` | `strict` | See above |
| `MEESHO_AFFILIATE_TEMPLATE` | empty | e.g. `https://www.meesho.com/{slug}/p/{id}?ref=YOU` |

## Notes

- Meesho changes markup and runs bot protection; extraction uses DOM-first,
  `__NEXT_DATA__`-fallback strategies. Expect to revisit selectors if Meesho
  ships a redesign. When blocked, `/trending` returns a warning inside the
  payload instead of crashing.
- One scrape runs at a time (single browser, small host).
