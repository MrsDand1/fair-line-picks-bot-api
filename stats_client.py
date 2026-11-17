"""Stage 2 stats client — team averages from ESPN's public site API.

Free, no key, no payment. Verified live Nov 17, 2026 for all four sports
(design §1.1). Two endpoints are used:

  teams:    /apis/site/v2/sports/{league}/teams?limit=500
  schedule: /apis/site/v2/sports/{league}/teams/{id}/schedule

Points-for AND points-against per game both come from the schedule
endpoint: it lists every completed game with both scores. ESPN's team
stats endpoint gives points-for but NOT points-against for NBA/NFL/NCAAB,
so the schedule is the single source for both — one call per team instead
of two, and the two numbers always agree with each other.

LOOKAHEAD RULE: averages are computed from completed games with an
America/New_York date strictly BEFORE the cycle date. A game played on
the cycle day itself never enters that day's averages, even if it finished
before the capture ran. That is what makes the numbers "as of game day".

Only teams that appear in the cycle's Kalshi markets are fetched — the
rule can only evaluate markets we have prices for, and NCAAB has ~365
teams, most of them with no open market on any given day.

POLITENESS: calls are spaced 0.2s apart with retries, never in a burst.
"""
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import config

USER_AGENT = "Mozilla/5.0"
# NOTE (verified live Nov 17, 2026): ESPN's WAF returns HTTP 403 for a
# custom bot User-Agent ("fair-line-picks-bot/stage2" -> 403 three times
# in a row) while a plain browser UA returns 200. The endpoint is public
# and unauthenticated; the UA is only what the WAF accepts.
MAX_RETRIES = 3
RETRY_BACKOFF_S = [5, 15, 30]


class EspnError(Exception):
    pass


def _get(url, retries=MAX_RETRIES):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_exc = EspnError(f"HTTP {e.code} on {url}")
            if attempt < retries:
                time.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
                continue
            raise last_exc
        except urllib.error.URLError as e:
            last_exc = EspnError(f"URL error on {url}: {e}")
            if attempt < retries:
                time.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
                continue
            raise last_exc
    raise last_exc


def list_teams(league_path):
    """All teams for one ESPN league. Returns raw team dicts."""
    url = (
        f"{config.ESPN_BASE}/{league_path}/teams"
        f"?limit={config.ESPN_TEAMS_LIMIT}"
    )
    data = _get(url)
    teams = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    return [t["team"] for t in teams if "team" in t]


def fetch_schedule(league_path, team_id):
    """Full-season schedule (regular season) for one team. Returns events."""
    url = f"{config.ESPN_BASE}/{league_path}/teams/{team_id}/schedule"
    data = _get(url)
    return data.get("events", [])


def fetch_scoreboard(league_path, yyyymmdd):
    """Dated scoreboard for one league/day. Returns events (with scores
    for completed games). Used by settlement, not by capture."""
    url = f"{config.ESPN_BASE}/{league_path}/scoreboard?dates={yyyymmdd}"
    data = _get(url)
    return data.get("events", [])


def event_et_date(iso_date):
    """America/New_York calendar date (YYYY-MM-DD) of an ESPN event.

    ESPN event dates are UTC ISO strings; a 7:30 PM ET game lands after
    midnight UTC, so the UTC date is NOT the game date. Convert first.
    """
    dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")


def competitor_score(c):
    """A competitor's score as a float, or None.

    ESPN uses TWO shapes: the schedule endpoint gives a dict
    {"value": 132.0, "displayValue": "132"}, while the scoreboard
    endpoint gives a plain string ("113"). Both are real; handle both.
    """
    s = c.get("score")
    v = s.get("value") if isinstance(s, dict) else s
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _completed_scores(events, team_id):
    """(team_scores, opponent_scores) for completed games in a schedule.

    Completed = competition status type state == "post". Returns raw
    score lists; averaging and as-of filtering happen in the caller so
    the same parse serves both capture and backtest reconstruction.
    """
    team_scores = []
    opp_scores = []
    dates = []
    for event in events:
        comps = event.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status = (comp.get("status") or {}).get("type") or {}
        if status.get("state") != "post":
            continue
        mine = None
        theirs = None
        for c in comp.get("competitors") or []:
            score = competitor_score(c)
            if score is None:
                continue
            if str((c.get("team") or {}).get("id")) == str(team_id):
                mine = score
            else:
                theirs = score
        if mine is None or theirs is None:
            continue
        team_scores.append(mine)
        opp_scores.append(theirs)
        dates.append(event_et_date(event.get("date", "")))
    return team_scores, opp_scores, dates


def parse_schedule_averages(events, team_id, as_of_date):
    """PF/G and PA/G from completed games with ET date < as_of_date.

    as_of_date is a YYYY-MM-DD string (the cycle date). Games played ON
    that date are excluded even if finished before the capture ran.
    Returns (games_played, pf_per_game, pa_per_game) — averages are None
    when no completed game qualifies.
    """
    team_scores, opp_scores, dates = _completed_scores(events, team_id)
    keep = [i for i, d in enumerate(dates) if d < as_of_date]
    n = len(keep)
    if n == 0:
        return 0, None, None
    pf = sum(team_scores[i] for i in keep) / n
    pa = sum(opp_scores[i] for i in keep) / n
    return n, round(pf, 4), round(pa, 4)


def find_game_in_schedule(events, team_id, game_date_et, opponent_abbr):
    """Home/away for one game from a team's schedule.

    Returns "home"/"away" for the team on the game dated game_date_et
    (ET) against the opponent with the given ESPN abbreviation, or None
    when the game is not found. Fallback for when the Kalshi ticker
    slug cannot be split into abbreviations.
    """
    for event in events:
        if event_et_date(event.get("date", "")) != game_date_et:
            continue
        comps = event.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        mine = None
        opp_ok = False
        for c in comp.get("competitors") or []:
            team = c.get("team") or {}
            if str(team.get("id")) == str(team_id):
                mine = c.get("homeAway")
            elif (team.get("abbreviation") or "").upper() == opponent_abbr.upper():
                opp_ok = True
        if mine and opp_ok:
            return mine
    return None


# --- team-name matching -----------------------------------------------------
# Kalshi market titles say e.g. "Atlanta wins", "Los Angeles C wins",
# "New York G wins", "St. Louis wins". ESPN teams carry location, name,
# displayName, shortDisplayName and abbreviation. Matching is scoped per
# league (an NBA "New York" can only be the Knicks) and keys are
# normalised the same way on both sides.

def normalize_name(s):
    """Lowercase, strip diacritics ("San José" -> "san jose"), strip
    periods, hyphens -> spaces, collapse whitespace. Parentheticals are
    KEPT as words ("Miami (FL)" -> "miami fl", "Miami (OH)" -> "miami
    oh") so the two Miamis never collide; match_title tries the
    parenthetical-free form as a fallback."""
    s = unicodedata.normalize("NFKD", s or "")
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.replace(".", "").replace("-", " ").replace("(", " ").replace(")", " ")
    return re.sub(r"\s+", " ", s.strip().lower())


# ESPN location abbreviations that Kalshi spells out: the Clippers'
# ESPN location is "LA" while Kalshi titles say "Los Angeles C".
LOCATION_ALIASES = {"la": "los angeles"}


def location_keys(location, name):
    """Location-based match keys for one team, aliases included."""
    location = normalize_name(location)
    name = normalize_name(name)
    keys = set()
    if not location:
        return keys
    keys.add(location)
    if name:
        keys.add(f"{location} {name[0]}")
    alias = LOCATION_ALIASES.get(location)
    if alias:
        keys.add(alias)
        if name:
            keys.add(f"{alias} {name[0]}")
    return keys


def team_match_keys(team):
    """PRIMARY match keys for one ESPN team (exact names only).

    Secondary keys (display-name prefixes, "X St.") live in
    secondary_match_keys and are only added when unclaimed — a prefix
    like "portland" is shared by Portland and Portland State, so adding
    it blindly poisons the exact location key.
    """
    location = normalize_name(team.get("location"))
    name = normalize_name(team.get("name"))
    keys = set()
    for field in ("abbreviation", "displayName", "shortDisplayName", "location", "name"):
        v = normalize_name(team.get(field))
        if v:
            keys.add(v)
    keys |= location_keys(location, name)
    # "Worcester St." (Kalshi) vs "Worcester State" (ESPN location).
    if location.endswith(" state"):
        keys.add(location[: -len(" state")] + " st")
    return keys


def secondary_match_keys(team):
    """SECONDARY keys: display-name prefixes, a bare "X St." form, and
    the name-expansion / first-word-drop forms below.

    "Southeastern Louisiana Lions" -> "southeastern", "southeastern
    louisiana"; "Grambling" (location) -> "grambling st" for the Kalshi
    label "Grambling St." These are only used when no primary key
    claims them (see build_team_map).
    """
    location = normalize_name(team.get("location"))
    display = normalize_name(team.get("displayName"))
    keys = set()
    if display:
        words = display.split(" ")
        for i in range(1, len(words)):
            keys.add(" ".join(words[:i]))
    if location:
        keys.add(f"{location} st")
    # ESPN abbreviations of full words: "UL Monroe Warhawks" (ESPN) vs
    # "Louisiana-Monroe" (Kalshi); "SE Louisiana Lions" vs
    # "Southeastern Louisiana". Expand + take prefixes of the expansion.
    for source in (display,):
        if source.startswith("ul "):
            expanded = "louisiana " + source[3:]
            _add_prefixes(keys, expanded)
        if source.startswith("se "):
            expanded = "southeastern " + source[3:]
            _add_prefixes(keys, expanded)
    # First-word drop: "LSU New Orleans Privateers" (ESPN) vs
    # "New Orleans" (Kalshi) — the label is the tail of the name.
    for source in (display, location):
        if source:
            words = source.split(" ")
            if len(words) > 1:
                _add_prefixes(keys, " ".join(words[1:]), include_full=True)
    return keys


def _add_prefixes(keys, text, include_full=False):
    words = text.split(" ")
    for i in range(1, len(words) + (1 if include_full else 0)):
        keys.add(" ".join(words[:i]))


def build_key_map(key_sets):
    """Two-pass key map. key_sets: iterable of (item, primary, secondary).

    Pass 1: primary keys, ambiguity-marked when two items claim one.
    Pass 2: secondary keys, added ONLY when unclaimed in pass 1 (and
    ambiguity-marked among themselves). A secondary key never overrides
    a primary key or an ambiguity.
    """
    mapping = {}
    ambiguous = set()
    for item, primary, _secondary in key_sets:
        for key in primary:
            if key in mapping and mapping[key] is not None \
                    and mapping[key] is not item:
                ambiguous.add(key)
            else:
                mapping.setdefault(key, item)
    for key in ambiguous:
        mapping[key] = None
    secondary = {}
    sec_amb = set()
    for item, _primary, second in key_sets:
        for key in second:
            if key in mapping:
                continue
            if key in secondary and secondary[key] is not item:
                sec_amb.add(key)
            else:
                secondary.setdefault(key, item)
    for key, item in secondary.items():
        if key not in sec_amb:
            mapping[key] = item
    return mapping


def build_team_map(teams):
    """key -> team for one league (two-pass; see build_key_map)."""
    return build_key_map(
        [(t, team_match_keys(t), secondary_match_keys(t)) for t in teams])


def match_title(title, team_map):
    """Match a Kalshi market title ("Atlanta wins") to an ESPN team.

    Returns (team_or_None, reason). reason is None on a clean match.

    Tries the label as-is first, then with any parenthetical removed:
    "Miami (FL)" -> "miami fl" (no claim) -> "miami" (Miami Hurricanes),
    while "Miami (OH)" matches Miami (OH) on the first try because
    ESPN's own location keeps the "(OH)". A key claimed by two teams
    is ambiguous and returns no team — never a silent wrong pick.
    """
    raw = re.sub(r"\s+wins\s*$", "", title or "", flags=re.I).strip()
    candidates = [normalize_name(raw)]
    stripped = normalize_name(re.sub(r"\([^)]*\)", " ", raw))
    if stripped and stripped != candidates[0]:
        candidates.append(stripped)
    for cand in candidates:
        if not cand:
            continue
        if cand in team_map:
            team = team_map[cand]
            return (team, None) if team is not None else (None, "ambiguous_team")
    return None, "no_stats"


# --- ticker parsing ---------------------------------------------------------

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_game_slug(ticker):
    """Split a Kalshi game ticker into (game_date, game_slug).

    Ticker shape: {SERIES}-{YY}{MON}{DD}{AWAY}{HOME}-{SIDE}
    e.g. KXNBAGAME-26NOV19PORMIL-MIL -> ("2026-11-19", "PORMIL").
    Returns (None, None) when the shape does not parse.
    """
    try:
        body = ticker.split("-", 1)[1]
        game_part = body.rsplit("-", 1)[0]
        m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})(.*)$", game_part)
        if not m:
            return None, None
        yy, mon, dd, rest = m.groups()
        month = MONTHS.get(mon)
        if not month:
            return None, None
        return f"20{yy}-{month:02d}-{int(dd):02d}", rest
    except (IndexError, ValueError):
        return None, None


def split_home_away(remainder, abbr_a, abbr_b):
    """Which abbreviation is away / home from the ticker slug remainder.

    The slug is away-abbreviation + home-abbreviation. Given both teams'
    abbreviations, exactly one ordering matches (unless they are equal).
    Returns (away_abbr, home_abbr) or None when neither ordering fits.
    """
    a, b = (abbr_a or "").upper(), (abbr_b or "").upper()
    if not a or not b or a == b:
        return None
    if remainder.upper() == a + b:
        return a, b
    if remainder.upper() == b + a:
        return b, a
    return None


# --- capture ----------------------------------------------------------------

def capture_stats(conn, cycle_date, markets=None):
    """Fetch team averages for every team in the cycle's Kalshi markets.

    markets: list of dicts with ticker/series/market_title; defaults to
    reading kalshi_snapshots for cycle_date from the DB. One row per team
    per cycle day in stats_snapshots (INSERT OR IGNORE — a resumed run
    never duplicates). Returns a summary dict.
    """
    import store  # local import: store imports config only, no cycle

    if markets is None:
        rows = conn.execute(
            "SELECT ticker, series, market_title FROM kalshi_snapshots "
            "WHERE cycle_date = ?",
            (cycle_date,),
        ).fetchall()
        markets = [dict(r) for r in rows]
    if not markets:
        return {"teams_matched": 0, "teams_unmatched": 0, "stored": 0,
                "already": 0, "unmatched_titles": [], "notes": ["no markets for cycle"]}

    done = store.already_captured_stats(conn, cycle_date)
    matched = {}       # (series, espn_team_id) -> team dict
    unmatched_titles = []
    by_series = {}
    for m in markets:
        by_series.setdefault(m["series"], []).append(m)

    for series, series_markets in by_series.items():
        league = config.ESPN_LEAGUES.get(series)
        if not league:
            unmatched_titles.extend(m["market_title"] for m in series_markets)
            continue
        teams = list_teams(league)
        team_map = build_team_map(teams)
        for m in series_markets:
            team, _reason = match_title(m.get("market_title") or "", team_map)
            if team is None:
                unmatched_titles.append(m.get("market_title"))
                continue
            matched[(series, str(team["id"]))] = team
        time.sleep(config.ESPN_DELAY_BETWEEN_CALLS_S)

    stored = already = 0
    for (series, espn_id), team in sorted(matched.items()):
        if (espn_id, series) in done:
            already += 1
            continue
        league = config.ESPN_LEAGUES[series]
        events = fetch_schedule(league, espn_id)
        games, pf, pa = parse_schedule_averages(events, espn_id, cycle_date)
        if store.insert_stats_snapshot(conn, {
            "espn_team_id": espn_id,
            "team_name": team.get("displayName"),
            "team_abbreviation": team.get("abbreviation"),
            "sport": series,
            "cycle_date": cycle_date,
            "fetched_at": store.utcnow_iso(),
            "games_played": games,
            "pf_per_game": pf,
            "pa_per_game": pa,
        }):
            stored += 1
        time.sleep(config.ESPN_DELAY_BETWEEN_CALLS_S)

    return {
        "teams_matched": len(matched),
        "teams_unmatched": len(set(unmatched_titles)),
        "stored": stored,
        "already": already,
        "unmatched_titles": sorted(set(unmatched_titles)),
        "notes": [],
    }
