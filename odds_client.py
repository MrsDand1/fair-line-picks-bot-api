"""The Odds API client (free tier).

The API key comes ONLY from the ODDS_API_KEY environment variable at runtime.
It is never in a file, in code, in a log, or in a test fixture. If the variable
is absent and stub mode is off, the client refuses to call anything.

Cost: one h2h call per sport per day with regions=us and
bookmakers=fanduel,draftkings,betmgm,pinnacle costs 1 credit
(markets x regions, bookmakers param = 1 region-equivalent; Stage 0).
4 sports/day = 4 credits/day, ~120/month of the 500 free credits.
No historical pulls (paid-only) and no spreads/totals scanning on free.

Stub mode (ODDS_API_STUB=1) replays a local fixture through the SAME parse
and store path, so tests exercise the real code without any key. The stub
fixture lives in tests/fixtures/ and contains fake data only.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import config


class OddsApiError(Exception):
    pass


def _headers_used(resp):
    try:
        return int(resp.headers.get("x-requests-used"))
    except (TypeError, ValueError):
        return None


def _headers_remaining(resp):
    try:
        return int(resp.headers.get("x-requests-remaining"))
    except (TypeError, ValueError):
        return None


def fetch_odds(sport_key, api_key):
    """Real call. Returns (events, credits_used, requests_remaining)."""
    params = urllib.parse.urlencode({
        "regions": config.ODDS_REGIONS,
        "markets": config.ODDS_MARKETS,
        "bookmakers": config.ODDS_BOOKMAKERS,
        "oddsFormat": "american",
        "apiKey": api_key,
    })
    url = f"{config.ODDS_BASE}/sports/{sport_key}/odds/?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "fair-line-picks-bot/stage1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            events = json.loads(resp.read())
            used = _headers_used(resp)
            remaining = _headers_remaining(resp)
    except urllib.error.HTTPError as e:
        raise OddsApiError(f"HTTP {e.code} on {url}: {e.read().decode()[:200]}")
    return events, used, remaining


def fetch_odds_stub(sport_key):
    """Stub call: replay the local fixture, same shape as the real API.

    Credits are simulated exactly as the real call would cost them:
    1 credit per sport call, remaining decremented from 500.
    """
    fixture = os.path.join(
        os.path.dirname(__file__), "tests", "fixtures", "odds_stub.json"
    )
    with open(fixture) as f:
        payload = json.load(f)
    events = payload["events"]
    return events, 1, 500 - 1


def get_odds(sport_key):
    """Entry point. Stub mode if ODDS_API_STUB=1, else real call with env key."""
    if os.environ.get("ODDS_API_STUB") == "1":
        return fetch_odds_stub(sport_key)
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        raise OddsApiError(
            "ODDS_API_KEY is not set and ODDS_API_STUB is not 1. "
            "Set ODDS_API_KEY at deploy time (see README) or run with "
            "ODDS_API_STUB=1 for the fixture replay. Never put the key in a file."
        )
    return fetch_odds(sport_key, api_key)


def parse_outcomes(events):
    """Flatten events -> outcome rows for storage."""
    outcomes = []
    for ev in events:
        for book in ev.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    price = outcome.get("price")
                    outcomes.append({
                        "event_id": ev.get("id"),
                        "commence_time": ev.get("commence_time"),
                        "home_team": ev.get("home_team"),
                        "away_team": ev.get("away_team"),
                        "bookmaker": book.get("key"),
                        "outcome_name": outcome.get("name"),
                        "price_american": price,
                        "price_decimal": _american_to_decimal(price),
                    })
    return outcomes


def _american_to_decimal(price):
    if price is None:
        return None
    if price > 0:
        return 1.0 + price / 100.0
    return 1.0 + 100.0 / abs(price)
