"""Fetch the nearest NBA or WNBA game match on Polymarket."""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
LEAGUES = ("nba", "wnba")


def get(path, **params):
    url = GAMMA + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "PredMarketQuoter/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def parse_time(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def series_ids():
    ids = {}
    for sport in get("/sports"):
        code = sport.get("sport")
        if code in LEAGUES:
            ids[code] = sport["series"]
    if not ids:
        raise RuntimeError("NBA/WNBA series not found")
    return ids


def is_game_match(event):
    if event.get("closed") or event.get("ended") or event.get("parentEventId"):
        return False
    types = {m.get("sportsMarketType") for m in event.get("markets") or []}
    return "moneyline" in types


def game_markets(event):
    out = []
    for market in event.get("markets") or []:
        kind = market.get("sportsMarketType")
        if kind in ("moneyline"):
            out.append({
                "type": kind,
                "question": market.get("question"),
                "slug": market.get("slug"),
                "line": market.get("line"),
                "clobTokenIds": market.get("clobTokenIds"),
                "outcomes": market.get("outcomes"),
            })
    return out


def fetch_league_games(league, series_id):
    events = get(
        "/events",
        series_id=series_id,
        closed="false",
        active="true",
        order="startTime",
        ascending="true",
        limit=100,
    )
    games = []
    for event in events:
        if is_game_match(event):
            event["_league"] = league
            games.append(event)
    return games


def fetch_nearest_nba_event():
    now = datetime.now(timezone.utc)
    matches = []
    for league, series_id in series_ids().items():
        matches.extend(fetch_league_games(league, series_id))
    if not matches:
        raise RuntimeError("No open NBA/WNBA games listed on Polymarket right now")

    def sort_key(event):
        start = parse_time(event.get("startTime")) or datetime.max.replace(tzinfo=timezone.utc)
        live = event.get("live") is True or start <= now
        return (0 if live else 1, abs((start - now).total_seconds()))

    return min(matches, key=sort_key)


if __name__ == "__main__":
    event = fetch_nearest_nba_event()
    league = event.get("_league")
    slug = event.get("slug")
    print(json.dumps({
        "league": league,
        "id": event.get("id"),
        "slug": slug,
        "title": event.get("title"),
        "startTime": event.get("startTime"),
        "live": event.get("live"),
        "url": f"https://polymarket.com/sports/{league}/{slug}",
        "markets": game_markets(event),
    }, indent=2))
