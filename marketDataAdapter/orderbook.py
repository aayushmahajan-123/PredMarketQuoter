"""In-memory CLOB and sportsbook odds books."""

from dataclasses import dataclass, field


@dataclass
class OrderBook:
    asset_id: str
    market: str = ""
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    timestamp: str | None = None
    hash: str | None = None

    def apply_snapshot(self, msg):
        self.market = str(msg.get("market") or self.market)
        self.timestamp = msg.get("timestamp")
        self.hash = msg.get("hash")
        self.bids = {lvl["price"]: float(lvl["size"]) for lvl in msg.get("bids") or []}
        self.asks = {lvl["price"]: float(lvl["size"]) for lvl in msg.get("asks") or []}

    def apply_price_changes(self, changes):
        for change in changes:
            price = str(change.get("price", ""))
            size = float(change.get("size") or 0)
            side = str(change.get("side") or "").upper()
            book = self.bids if side == "BUY" else self.asks
            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size

    def best_bid(self):
        return max((float(p) for p in self.bids), default=None)

    def best_ask(self):
        return min((float(p) for p in self.asks), default=None)

    def liquidity_mid(self):
        bid_px = 0.0
        bid_sz = 0.0
        for price, size in self.bids.items():
            bid_px += float(price) * size
            bid_sz += size
        ask_px = 0.0
        ask_sz = 0.0
        for price, size in self.asks.items():
            ask_px += float(price) * size
            ask_sz += size
        if bid_sz <= 0 and ask_sz <= 0:
            return None
        if bid_sz <= 0:
            return ask_px / ask_sz
        if ask_sz <= 0:
            return bid_px / bid_sz
        bid_vwap = bid_px / bid_sz
        ask_vwap = ask_px / ask_sz
        return (bid_vwap * ask_sz + ask_vwap * bid_sz) / (bid_sz + ask_sz)

    def summary(self):
        return (
            f"asset={self.asset_id[:12]}... bid={self.best_bid()} ask={self.best_ask()} "
            f"levels={len(self.bids)}/{len(self.asks)}"
        )

    def top_levels(self, n=5):
        bids = sorted(self.bids.items(), key=lambda x: float(x[0]), reverse=True)[:n]
        asks = sorted(self.asks.items(), key=lambda x: float(x[0]))[:n]
        return bids, asks


def american_to_price(odds):
    """Convert American odds to a Polymarket-style 0-1 price (implied probability)."""
    odds = float(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


@dataclass
class OddsBook:
    """Bovada moneyline for the matched Polymarket game."""

    bookmaker: str
    event_id: str = ""
    home: str = ""
    away: str = ""
    lines: dict = field(default_factory=dict)
    prices: dict = field(default_factory=dict)
    books: dict = field(default_factory=dict)
    last_update: str | None = None

    def apply_h2h(self, event, bookmaker):
        self.event_id = str(event.get("id") or self.event_id)
        self.home = event.get("home_team") or self.home
        self.away = event.get("away_team") or self.away
        self.bookmaker = bookmaker.get("key") or self.bookmaker
        self.last_update = bookmaker.get("last_update")
        self.lines = {}
        self.prices = {}
        self.books = {}
        for market in bookmaker.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes") or []:
                name = outcome.get("name")
                american = outcome.get("price")
                if name is None or american is None:
                    continue
                name = str(name)
                price = round(american_to_price(american), 4)
                self.lines[name] = float(american)
                self.prices[name] = price
                book = OrderBook(asset_id=f"bovada:{name}")
                book.asks = {str(price): 1.0}
                self.books[name] = book

    def summary(self):
        parts = []
        for name, price in self.prices.items():
            american = self.lines.get(name)
            parts.append(f"{name} {price:.4f} ({price * 100:.1f}%) {american:+g}")
        return f"{self.bookmaker} {' | '.join(parts)} updated={self.last_update}"
