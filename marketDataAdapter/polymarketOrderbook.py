"""Subscribe to Polymarket CLOB orderbooks for a given event."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import websockets
from websockets.exceptions import ConnectionClosed

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from orderbook import OrderBook

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_S = 10
EVENT_URL_RE = re.compile(
    r"polymarket\.com/(?:event|events)/(?P<slug>[^/?#]+)",
    re.IGNORECASE,
)


def _json_get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "PredMarketQuoter/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            parsed = json.loads(text)
            return [str(v) for v in parsed]
        return [text]
    return [str(value)]


def parse_event_ref(event: Union[str, int]) -> Dict[str, str]:
    """Accept an event id, slug, or polymarket.com/event/<slug> URL."""
    if isinstance(event, int) or (isinstance(event, str) and event.strip().isdigit()):
        return {"kind": "id", "value": str(event).strip()}

    text = str(event).strip()
    match = EVENT_URL_RE.search(text)
    if match:
        return {"kind": "slug", "value": urllib.parse.unquote(match.group("slug"))}

    if text.startswith("http://") or text.startswith("https://"):
        slug = urllib.parse.urlparse(text).path.rstrip("/").split("/")[-1]
        return {"kind": "slug", "value": urllib.parse.unquote(slug)}

    return {"kind": "slug", "value": text}


def fetch_event(event: Union[str, int]) -> Dict[str, Any]:
    ref = parse_event_ref(event)
    if ref["kind"] == "id":
        url = f"{GAMMA_API}/events/{ref['value']}"
    else:
        url = f"{GAMMA_API}/events/slug/{urllib.parse.quote(ref['value'], safe='')}"
    try:
        payload = _json_get(url)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Event not found: {event!r} ({exc.code})") from exc
    if isinstance(payload, list):
        if not payload:
            raise ValueError(f"Event not found: {event!r}")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected event payload for {event!r}")
    return payload


def token_ids_from_event(event_payload: Dict[str, Any]) -> List[str]:
    token_ids: List[str] = []
    seen = set()
    for market in event_payload.get("markets") or []:
        ids = _as_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
        for token_id in ids:
            if token_id not in seen:
                seen.add(token_id)
                token_ids.append(token_id)
    if not token_ids:
        raise ValueError("Event has no CLOB token ids (closed or not listed yet)")
    return token_ids


class PolymarketOrderbook:
    """Resolve an event, then stream its outcome orderbooks over the market WS."""

    def __init__(self, event: Union[str, int]):
        self.event_ref = event
        self.event: Dict[str, Any] = {}
        self.token_ids: List[str] = []
        self.books: Dict[str, OrderBook] = {}

    def load_event(self) -> None:
        self.event = fetch_event(self.event_ref)
        self.token_ids = token_ids_from_event(self.event)
        self.books = {tid: OrderBook(asset_id=tid) for tid in self.token_ids}
        title = self.event.get("title") or self.event.get("slug") or self.event_ref
        logger.info("Loaded event %s with %d tokens", title, len(self.token_ids))

    def _handle_message(self, msg: Dict[str, Any]) -> None:
        event_type = msg.get("event_type") or msg.get("type")
        if event_type == "book":
            asset_id = str(msg.get("asset_id") or "")
            book = self.books.setdefault(asset_id, OrderBook(asset_id=asset_id))
            book.apply_snapshot(msg)
            logger.info("book %s", book.summary())
            return
        if event_type == "price_change":
            for change in msg.get("price_changes") or []:
                asset_id = str(change.get("asset_id") or msg.get("asset_id") or "")
                if not asset_id:
                    continue
                book = self.books.setdefault(asset_id, OrderBook(asset_id=asset_id))
                book.apply_price_changes([change])
            logger.debug("price_change %s", msg)
            return
        logger.debug("%s %s", event_type, msg)

    async def run(self) -> None:
        if not self.token_ids:
            self.load_event()

        subscribe = {
            "assets_ids": self.token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }

        async with websockets.connect(MARKET_WS, ping_interval=None) as ws:
            await ws.send(json.dumps(subscribe))
            logger.info("Subscribed to %d assets", len(self.token_ids))

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(PING_INTERVAL_S)
                    await ws.send("PING")

            hb = asyncio.create_task(heartbeat())
            try:
                async for raw in ws:
                    if raw == "PONG":
                        continue
                    payload = json.loads(raw)
                    if isinstance(payload, list):
                        for item in payload:
                            if isinstance(item, dict):
                                self._handle_message(item)
                    elif isinstance(payload, dict):
                        self._handle_message(payload)
            except ConnectionClosed:
                logger.warning("Market websocket closed")
                raise
            finally:
                hb.cancel()


async def stream_event(event: Union[str, int]) -> None:
    adapter = PolymarketOrderbook(event)
    adapter.load_event()
    await adapter.run()


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fetch live Polymarket orderbooks for an event (id, slug, or URL)."
    )
    parser.add_argument("event", help="Event id, slug, or polymarket.com/event/<slug> URL")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(stream_event(args.event))


if __name__ == "__main__":
    main()
