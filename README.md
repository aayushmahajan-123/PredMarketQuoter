# PredMarketQuoter

Quotes NBA/WNBA moneylines by combining **Polymarket CLOB** books with **Bovada** implied odds (via The Odds API). The strategy builds a two-sided ladder on team A and mirrors it for team B as `1 - price`.

This is a quoter / research loop. It does **not** place orders.

## What it does

1. Picks the nearest open NBA or WNBA **moneyline** event on Polymarket.
2. Loads that event’s two outcome tokens (team names) from the CLOB REST book.
3. Pulls Bovada h2h for the same match and converts American odds to 0–1 prices.
4. Blends Polymarket mid and Bovada mid, applies inventory skew and time-to-expiry spread.
5. Posts a 5-level triangle book on each side (size heaviest at the touch).
6. Reloads inventory from `inventory.json` every loop so a second process can update position.

## Setup

```bash
cd PredMarketQuoter
pip install -r requirements.txt
```

Create `.env` (already gitignored):

```
ODDS_API_KEY=your_the_odds_api_key
```

Bovada moneyline comes from [The Odds API](https://the-odds-api.com) (`bookmakers=bovada`, `markets=h2h`). Free tiers are often NBA-only; WNBA may 403 until you upgrade.

## Run

Terminal 1 — quoter (continuous):

```bash
python3 main.py
```

Terminal 2 — inventory:

```bash
python3 update_inventory.py
> A 50
> B 10
```

Or one-shot:

```bash
python3 update_inventory.py A 50
python3 update_inventory.py B 10
```

**A** adds to `current_position` (long team A). **B** subtracts (long team B). Position is stored in `inventory.json`.

## Layout

```
PredMarketQuoter/
  main.py                         # Strategy + quote loop
  update_inventory.py             # CLI inventory updates
  inventory.json                  # shared position
  helpers/fetchNearestNbaEventPoly.py
  marketDataAdapter/
    MarketAdapter.py              # Polymarket + Bovada books
    orderbook.py                  # OrderBook, OddsBook, liquidity mid
    polymarketOrderbook.py        # websocket helper (optional)
```

## Quote logic (short)

- **Mid:** `(polymarket_mid + bovada_mid) / 2` on team A.
- **Spread:** `minspread + timeToExpirySkew * (1/tte)^exponent`.
- **Inventory skew:** `net = position / maxpos`.  
  `skew = sensitivity * net`, with extra `|sensitivity|` after `inventoryUrgencyThreshold` via `sensitivityExponent`.
- **Team B:** bid/ask and sizes are the complement of team A (`1 - price`).
- **Sizes:** `quoteVolume` split 50/50, then shifted by position  
  (e.g. volume 500, long 50 of A → bid 200 / ask 300).
- **Levels:** at least 5 bids and 5 asks; size tapers away from the touch.

Pretty-print shows asks (high → low), spread, then bids.

## Parameters (`main.py`)

| Param | Role |
|---|---|
| `maxpos` | Position scale for `net` |
| `minspread` / `maxspread` | Quote width (price units, 0–1) |
| `sensitivity` | Inventory shift of the whole book (negative fades a long) |
| `inventoryUrgencyThreshold` | `|net|` where sensitivity starts ramping |
| `sensitivityExponent` | How hard that ramp is |
| `timeToExpirySkew` / `Exponent` | Widen into settlement |
| `quoteVolume` | Total size to post |
| `levels` | Rungs per side (minimum 5) |

## Notes

- Polymarket books are keyed by **team name**, same as Bovada (`Connecticut Sun`, `Atlanta Dream`).
- A token at ~8¢ bid / ~13¢ ask is normal for a longshot; the other token should sit near `1 - that`.
- `tte` is seconds to Polymarket `endDate` (event close / settlement), not tip-off.
- Do not commit `.env`. Rotate the Odds API key if it was ever pasted in chat.
