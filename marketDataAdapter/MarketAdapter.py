import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "helpers"))

from fetchNearestNbaEventPoly import fetch_nearest_nba_event, game_markets
from orderbook import OddsBook, OrderBook

MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_API = "https://clob.polymarket.com"
ODDS_API = "https://api.the-odds-api.com/v4/sports"
ODDS_POLL_S = 60*60
SPORT_KEYS = {"nba": "basketball_nba", "wnba": "basketball_wnba"}


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            return [str(v) for v in json.loads(text)]
        return [text]
    return [str(value)]


def token_ids_from_markets(markets):
    ids = []
    for market in markets:
        for token_id in as_list(market.get("clobTokenIds")):
            ids.append(token_id)
    return ids


def token_outcome_map(markets):
    mapping = {}
    for market in markets:
        ids = as_list(market.get("clobTokenIds"))
        names = as_list(market.get("outcomes"))
        for i, token_id in enumerate(ids):
            name = names[i] if i < len(names) else token_id
            mapping[token_id] = name
    return mapping


def names_from_title(title):
    text = (title or "").replace(" vs. ", " vs ").replace(" @ ", " vs ")
    if " vs " not in text:
        return []
    return [p.strip().lower() for p in text.split(" vs ", 1)]


def event_matches(odds_event, poly_title):
    parts = names_from_title(poly_title)
    if len(parts) != 2:
        return False
    blob = f"{odds_event.get('home_team','')} {odds_event.get('away_team','')}".lower()
    return all(p in blob for p in parts)


class MarketAdapter:
    def __init__(self):
        load_env()
        self.odds_api_key = os.environ.get("ODDS_API_KEY", "")
        self.event = fetch_nearest_nba_event()
        self.markets = game_markets(self.event)
        self.token_ids = token_ids_from_markets(self.markets)
        self.token_to_outcome = token_outcome_map(self.markets)
        self.books = {}
        for token_id in self.token_ids:
            name = self.token_to_outcome.get(token_id, token_id)
            self.books[name] = OrderBook(asset_id=token_id, market=name)
        self.odds_books = {}
        self.bovada_event_id = None
        print("event", self.event.get("title"), self.event.get("slug"))
        print("tokens", len(self.token_ids))
        self.refresh_polymarket_books()
        self.refresh_odds()

    def print_top(self, book, n=5):
        bids, asks = book.top_levels(n)
        label = book.market or book.asset_id
        print("polymarket" if not str(book.asset_id).startswith("bovada:") else "bovada", label)
        print("top 5 bids")
        for price, qty in bids:
            print(f"  {price}  {qty}")
        print("top 5 asks")
        for price, qty in asks:
            print(f"  {price}  {qty}")

    def refresh_polymarket_books(self):
        for token_id in self.token_ids:
            url = f"{CLOB_API}/book?{urllib.parse.urlencode({'token_id': token_id})}"
            req = urllib.request.Request(url, headers={"User-Agent": "PredMarketQuoter/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    payload = json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                print("polymarket book error", token_id[:12], exc.code)
                continue
            name = self.token_to_outcome.get(token_id, token_id)
            book = self.books.setdefault(name, OrderBook(asset_id=token_id, market=name))
            book.apply_snapshot(payload)
            self.print_top(book)

    def print_odds(self):
        if not self.odds_books:
            print("odds api: no books yet")
            return
        print("bovada moneyline", self.event.get("title"))
        for odds_book in self.odds_books.values():
            for name, book in odds_book.books.items():
                price = odds_book.prices.get(name, 0)
                print(f"  {name}  {price:.4f}  ({price * 100:.1f}%)")
                self.print_top(book)

    def refresh_odds(self):
        if not self.odds_api_key:
            print("odds api: missing ODDS_API_KEY")
            return
        league = self.event.get("_league")
        sport = SPORT_KEYS.get(league)
        if not sport:
            print("odds api: unknown league", league)
            return
        params = {
            "apiKey": self.odds_api_key,
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "american",
            "bookmakers": "bovada",
        }
        url = f"{ODDS_API}/{sport}/odds/?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "PredMarketQuoter/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                remaining = resp.headers.get("x-requests-remaining")
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            print("odds api error", exc.code, body[:300])
            return
        title = self.event.get("title")
        match = next((e for e in payload if event_matches(e, title)), None)
        if not match:
            print("bovada moneyline: no matching game for", title)
            return
        bovada = next((b for b in (match.get("bookmakers") or []) if b.get("key") == "bovada"), None)
        if not bovada:
            print("bovada moneyline: not listed for", title)
            return
        self.bovada_event_id = match.get("id")
        book = self.odds_books.setdefault("bovada", OddsBook(bookmaker="bovada"))
        book.apply_h2h(match, bovada)
        if remaining:
            print("odds api remaining", remaining)
        self.print_odds()

    def book_for_asset(self, asset_id):
        name = self.token_to_outcome.get(asset_id, asset_id)
        if name not in self.books:
            self.books[name] = OrderBook(asset_id=asset_id, market=name)
        return self.books[name]

    def handle(self, msg):
        kind = msg.get("event_type") or msg.get("type")
        if kind == "book":
            asset_id = str(msg.get("asset_id") or "")
            book = self.book_for_asset(asset_id)
            book.apply_snapshot(msg)
            self.print_top(book)
            return
        if kind == "price_change":
            updated = set()
            for change in msg.get("price_changes") or []:
                asset_id = str(change.get("asset_id") or msg.get("asset_id") or "")
                if not asset_id:
                    continue
                book = self.book_for_asset(asset_id)
                book.apply_price_changes([change])
                updated.add(book.market)
            for name in updated:
                self.print_top(self.books[name])

    async def poll_odds(self):
        while True:
            await asyncio.sleep(ODDS_POLL_S)
            try:
                self.refresh_odds()
            except Exception as exc:
                print("odds api poll failed", exc)

    async def run(self):
        async with websockets.connect(MARKET_WS, ping_interval=None) as ws:
            await ws.send(json.dumps({
                "assets_ids": self.token_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }))

            async def ping():
                while True:
                    await asyncio.sleep(10)
                    await ws.send("PING")

            hb = asyncio.create_task(ping())
            odds_task = asyncio.create_task(self.poll_odds())
            try:
                async for raw in ws:
                    if raw == "PONG":
                        continue
                    payload = json.loads(raw)
                    if isinstance(payload, list):
                        for item in payload:
                            self.handle(item)
                    else:
                        self.handle(payload)
            finally:
                hb.cancel()
                odds_task.cancel()


if __name__ == "__main__":
    adapter = MarketAdapter()
    asyncio.run(adapter.run())
