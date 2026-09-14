import json
import time
from datetime import datetime, timezone
from pathlib import Path

from marketDataAdapter.MarketAdapter import MarketAdapter

INVENTORY_PATH = Path(__file__).resolve().parent / "inventory.json"


class Strategy:
    def __init__(self):
        self.market_adapter = MarketAdapter()
        self.maxpos = 1000
        self.minspread = 0.05 ## bps
        self.maxspread = 0.2 ## bps
        self.sensitivity = -0.05 ## 1.0 = 100%
        self.timeToExpirySkew = 0.005 ## 1 bps 
        self.timeToExpirySkewExponent = 5

        self.bidQuotes = {}
        self.askQuotes = {}
        self.lastUpdateTime = None
        event = self.market_adapter.event
        self.expiryTime = event.get("endDate") or event.get("endDateIso")
        if not self.expiryTime:
            for market in event.get("markets") or []:
                if market.get("sportsMarketType") == "moneyline":
                    self.expiryTime = market.get("endDate") or market.get("endDateIso")
                    break
        if not self.expiryTime:
            self.expiryTime = event.get("startTime")

        self.current_position = 0 ## positive means asset A and negative means asset B
        self.inventoryUrgencyThreshold = 0.5
        self.sensitivityExponent = 2
        self.load_position()

        self.quoteVolume = 500
        self.levels = 5

    def load_position(self):
        if not INVENTORY_PATH.exists():
            return
        data = json.loads(INVENTORY_PATH.read_text())
        self.current_position = float(data.get("current_position", 0))

    def time_to_expiry(self):
        if not self.expiryTime:
            return 0
        text = str(self.expiryTime).replace("Z", "+00:00")
        end = datetime.fromisoformat(text)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        secs = (end - datetime.now(timezone.utc)).total_seconds()
        if secs < 0:
            return 0
        return secs

    def make_triangle(self, start, n, side, total_size):
        levels = []
        if n < 1 or total_size <= 0:
            return levels
        if side == "bid":
            step = start / n
        else:
            step = (1.0 - start) / n
        wsum = n * (n + 1) / 2.0
        i = 0
        while i < n:
            if side == "bid":
                px = start - i * step
            else:
                px = start + i * step
            if px > 0 and px < 1:
                w = n - i
                sz = total_size * (w / wsum)
                levels.append((round(px, 4), sz))
            i = i + 1
        return levels

    def print_orderbook(self, name, bids, asks, mid, pos, net, sens, skew):
        print("")
        print("=" * 44)
        print(" " + name)
        print(" mid " + str(round(mid, 4)) + "  pos " + str(pos) + "  net " + str(round(net, 4)))
        print(" skew " + str(round(skew, 6)) + "  sens " + str(round(sens, 6)))
        print("-" * 44)
        print("  SIDE     SIZE      PRICE")
        print("-" * 44)
        asks_hi = list(asks)
        asks_hi.reverse()
        for px, sz in asks_hi:
            print("  ASK   " + str(round(sz, 2)).rjust(8) + "   " + str(round(px, 4)).rjust(8))
        print("  ---------------- spread ----------------")
        for px, sz in bids:
            print("  BID   " + str(round(sz, 2)).rjust(8) + "   " + str(round(px, 4)).rjust(8))
        print("=" * 44)

    def create_quotes(self):
        self.load_position()
        tte = self.time_to_expiry()
        if tte <= 0:
            tte = 1
        self.bidQuotes = {}
        self.askQuotes = {}

        polymarket = self.market_adapter.books
        bovada = self.market_adapter.odds_books.get("bovada")
        bovada_prices = bovada.prices if bovada else {}
        teams = list(polymarket.keys())
        if len(teams) < 2:
            return

        team_a = teams[0]
        team_b = teams[1]
        poly_book = polymarket[team_a]

        poly_mid = None  #poly_book.liquidity_mid()
        if poly_mid is None:
            poly_bid = poly_book.best_bid()
            poly_ask = poly_book.best_ask()
            if poly_bid is not None and poly_ask is not None:
                poly_mid = (poly_bid + poly_ask) / 2
            else:
                poly_mid = poly_bid if poly_bid is not None else poly_ask

        bov_mid = bovada_prices.get(team_a)
        if poly_mid is None:
            poly_mid = bov_mid
        if bov_mid is None:
            bov_mid = poly_mid
        if poly_mid is None:
            return

        blended_mid = (poly_mid + bov_mid) / 2  

        new_mid = blended_mid
        new_spread = self.minspread + self.timeToExpirySkew * (1.0 / tte) ** self.timeToExpirySkewExponent
        new_half = new_spread / 2.0
        fair_bid = new_mid - new_half
        fair_ask = new_mid + new_half
        if fair_bid < 0:
            fair_bid = 0
        if fair_ask > 1:
            fair_ask = 1

        net_position = self.current_position / self.maxpos
        if net_position > 1:
            net_position = 1
        if net_position < -1:
            net_position = -1

        abs_net = abs(net_position)
        eff_sensitivity = self.sensitivity
        if abs_net > self.inventoryUrgencyThreshold:
            extra = abs_net - self.inventoryUrgencyThreshold
            room = 1.0 - self.inventoryUrgencyThreshold
            if room <= 0:
                room = 1
            u = extra / room
            eff_sensitivity = self.sensitivity * (1.0 + u ** self.sensitivityExponent)

        skew = eff_sensitivity * net_position

        skewed_bid = fair_bid + skew
        skewed_ask = fair_ask + skew
        if skewed_bid < 0:
            skewed_bid = 0
        if skewed_ask > 1:
            skewed_ask = 1

        half_size = self.quoteVolume / 2.0
        bid_size_a = half_size - self.current_position
        ask_size_a = half_size + self.current_position
        if bid_size_a < 0:
            bid_size_a = 0
        if ask_size_a < 0:
            ask_size_a = 0
        if bid_size_a > self.quoteVolume:
            bid_size_a = self.quoteVolume
        if ask_size_a > self.quoteVolume:
            ask_size_a = self.quoteVolume

        n_bid = self.levels
        n_ask = self.levels
        if n_bid < 5:
            n_bid = 5
        if n_ask < 5:
            n_ask = 5

        bids_a = self.make_triangle(skewed_bid, n_bid, "bid", bid_size_a)
        asks_a = self.make_triangle(skewed_ask, n_ask, "ask", ask_size_a)

        bids_b = []
        for px, sz in asks_a:
            bids_b.append((round(1.0 - px, 4), sz))
        asks_b = []
        for px, sz in bids_a:
            asks_b.append((round(1.0 - px, 4), sz))

        self.bidQuotes[team_a] = bids_a
        self.askQuotes[team_a] = asks_a
        self.bidQuotes[team_b] = bids_b
        self.askQuotes[team_b] = asks_b

        self.print_orderbook(team_a, bids_a, asks_a, new_mid, self.current_position, net_position, eff_sensitivity, skew)
        self.print_orderbook(team_b, bids_b, asks_b, 1.0 - new_mid, -self.current_position, -net_position, eff_sensitivity, -skew)

    def run(self):
        while True:
            self.market_adapter.refresh_polymarket_books()
            self.create_quotes()
            time.sleep(4)


Strategy().run()
