"""Kalshi public REST client (keyless, free).

Semantics verified in Stage 0 against Kalshi's own docs ("Orderbook
Responses", updated 2026-11-13) and cross-checked against kalshi.com's
displayed prices on 3 markets (exact match on the YES ask):
  YES bid = highest yes_dollars price (arrays sorted ascending)
  YES ask = 1 - highest no_dollars price
Only bids are returned; there is no ask field. depth=1 returns just the
best bid per side.

POLITENESS: Kalshi's keyless rate limit is undocumented and a burst from
this VM got HTTP 429 on Nov 16, 2026. Calls are spaced seconds apart and
a 429 is retried with backoff, never in a burst.
"""
import json
import time
import urllib.error
import urllib.request

import config


class KalshiError(Exception):
    pass


def _get(url, retries=config.MAX_RETRIES):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fair-line-picks-bot/stage1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_exc = KalshiError(f"HTTP {e.code} on {url}")
            if e.code == 429 and attempt < retries:
                wait = config.RETRY_BACKOFF_S[min(attempt, len(config.RETRY_BACKOFF_S) - 1)]
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(wait, int(retry_after))
                    except ValueError:
                        pass
                time.sleep(wait)
                continue
            raise last_exc
        except urllib.error.URLError as e:
            last_exc = KalshiError(f"URL error on {url}: {e}")
            if attempt < retries:
                time.sleep(config.RETRY_BACKOFF_S[min(attempt, len(config.RETRY_BACKOFF_S) - 1)])
                continue
            raise last_exc
    raise last_exc


def parse_orderbook(payload):
    """Derive top-of-book from an orderbook response payload.

    NOTE (live format, Nov 16, 2026): prices and counts arrive as STRINGS
    (e.g. "0.2800"), not numbers — coerce to float before comparing.
    """
    ob = payload.get("orderbook_fp") or payload.get("orderbook") or {}
    yes_levels = ob.get("yes_dollars") or []
    no_levels = ob.get("no_dollars") or []
    yes_bid = max((float(lvl[0]) for lvl in yes_levels), default=None)
    no_best = max((float(lvl[0]) for lvl in no_levels), default=None)
    yes_ask = (1.0 - no_best) if no_best is not None else None
    # Round to 4dp: Kalshi prices are whole cents, this drops float dust
    # like 0.29000000000000004 from the 1.0 - 0.71 subtraction.
    return {
        "yes_bid": round(yes_bid, 4) if yes_bid is not None else None,
        "yes_ask": round(yes_ask, 4) if yes_ask is not None else None,
        "yes_levels": len(yes_levels),
        "no_levels": len(no_levels),
    }


def list_open_markets(series_ticker, delay=config.KALSHI_DELAY_BETWEEN_SERIES_S):
    """All open markets for one series (paginated via cursor)."""
    markets = []
    cursor = None
    while True:
        url = (
            f"{config.KALSHI_BASE}/markets?series_ticker={series_ticker}"
            f"&status=open&limit=100"
        )
        if cursor:
            url += f"&cursor={cursor}"
        data = _get(url)
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(delay)
    return markets


def fetch_book(ticker, depth=1):
    """Top-of-book for one market. Returns parsed book + raw payload."""
    url = f"{config.KALSHI_BASE}/markets/{ticker}/orderbook?depth={depth}"
    payload = _get(url)
    return parse_orderbook(payload), payload
