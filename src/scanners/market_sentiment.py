"""
TradeSight Adanos market sentiment integration.

The client enriches a stock watchlist with compact market sentiment context from
multiple Adanos stock endpoints. It is fully optional and fails open when the
API key is missing or the service is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional
import logging

import requests

import config


logger = logging.getLogger(__name__)


@dataclass
class SourceSentimentSnapshot:
    source: str
    buzz_score: Optional[float] = None
    sentiment_score: Optional[float] = None
    bullish_pct: Optional[float] = None
    trend: Optional[str] = None
    activity_count: int = 0


@dataclass
class StockSentimentSnapshot:
    ticker: str
    company_name: str = ""
    average_buzz: Optional[float] = None
    average_sentiment_score: Optional[float] = None
    average_bullish_pct: Optional[float] = None
    coverage: int = 0
    source_alignment: str = "unavailable"
    sentiment_label: str = "unavailable"
    score_component: float = 50.0
    sources: Dict[str, SourceSentimentSnapshot] = field(default_factory=dict)


class MarketSentimentClient:
    """
    Batch client for Adanos compare endpoints.

    The scanner only needs compact watchlist metrics, so the compare endpoints
    are the right fit and keep the request count bounded to 4 requests per
    watchlist chunk instead of 4 requests per ticker.
    """

    SOURCE_PATHS = {
        "reddit": "/reddit/stocks/v1/compare",
        "x": "/x/stocks/v1/compare",
        "news": "/news/stocks/v1/compare",
        "polymarket": "/polymarket/stocks/v1/compare",
    }
    MAX_TICKERS_PER_REQUEST = 10

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = config.ADANOS_API_BASE_URL,
        timeout_seconds: int = config.ADANOS_TIMEOUT_SECONDS,
        default_days: int = config.ADANOS_SENTIMENT_DAYS,
    ):
        self.api_key = api_key or config.ADANOS_API_KEY
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.default_days = default_days
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "TradeSight/1.0 (Market Sentiment Overlay)",
            }
        )
        if self.api_key:
            self.session.headers["X-API-Key"] = self.api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def get_stock_sentiment(
        self,
        tickers: List[str],
        days: Optional[int] = None,
    ) -> Dict[str, StockSentimentSnapshot]:
        if not self.enabled:
            return {}

        cleaned = [ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()]
        if not cleaned:
            return {}

        snapshots: Dict[str, StockSentimentSnapshot] = {
            ticker: StockSentimentSnapshot(ticker=ticker) for ticker in cleaned
        }
        lookback_days = max(1, min(int(days or self.default_days), 90))

        for source, path in self.SOURCE_PATHS.items():
            for chunk in self._chunk(cleaned, self.MAX_TICKERS_PER_REQUEST):
                try:
                    rows = self._fetch_compare_rows(path, chunk, lookback_days)
                except requests.RequestException as exc:
                    logger.warning("Adanos %s compare request failed: %s", source, exc)
                    continue
                except ValueError as exc:
                    logger.warning("Adanos %s compare payload invalid: %s", source, exc)
                    continue

                for row in rows:
                    ticker = str(row.get("ticker", "")).upper()
                    if ticker not in snapshots:
                        continue
                    snapshots[ticker].sources[source] = self._normalize_source_row(source, row)
                    if row.get("company_name") and not snapshots[ticker].company_name:
                        snapshots[ticker].company_name = str(row["company_name"])

        return {
            ticker: self._finalize_snapshot(snapshot)
            for ticker, snapshot in snapshots.items()
            if snapshot.sources
        }

    def _fetch_compare_rows(self, path: str, tickers: List[str], days: int) -> List[Dict]:
        response = self.session.get(
            f"{self.base_url}{path}",
            params={"tickers": ",".join(tickers), "days": days},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        stocks = payload.get("stocks")
        if not isinstance(stocks, list):
            raise ValueError(f"Unexpected compare payload shape from {path}")
        return stocks

    def _normalize_source_row(self, source: str, row: Dict) -> SourceSentimentSnapshot:
        if source == "polymarket":
            activity_count = int(row.get("trade_count") or 0)
        else:
            activity_count = int(row.get("mentions") or 0)

        return SourceSentimentSnapshot(
            source=source,
            buzz_score=self._as_float(row.get("buzz_score")),
            sentiment_score=self._as_float(row.get("sentiment_score")),
            bullish_pct=self._as_float(row.get("bullish_pct")),
            trend=row.get("trend"),
            activity_count=activity_count,
        )

    def _finalize_snapshot(self, snapshot: StockSentimentSnapshot) -> StockSentimentSnapshot:
        buzz_values = [source.buzz_score for source in snapshot.sources.values() if source.buzz_score is not None]
        sentiment_values = [
            source.sentiment_score
            for source in snapshot.sources.values()
            if source.sentiment_score is not None
        ]
        bullish_values = [
            source.bullish_pct for source in snapshot.sources.values() if source.bullish_pct is not None
        ]

        snapshot.coverage = len(snapshot.sources)
        snapshot.average_buzz = self._average(buzz_values)
        snapshot.average_sentiment_score = self._average(sentiment_values)
        snapshot.average_bullish_pct = self._average(bullish_values)
        snapshot.source_alignment = self._compute_alignment(snapshot.sources.values())
        snapshot.sentiment_label = self._compute_label(
            snapshot.average_sentiment_score,
            snapshot.average_bullish_pct,
        )
        snapshot.score_component = self._score_component(snapshot)
        return snapshot

    def _score_component(self, snapshot: StockSentimentSnapshot) -> float:
        score = 50.0
        if snapshot.average_sentiment_score is not None:
            score += snapshot.average_sentiment_score * 22.0
        if snapshot.average_bullish_pct is not None:
            score += (snapshot.average_bullish_pct - 50.0) * 0.55
        if snapshot.average_buzz is not None:
            score += (snapshot.average_buzz - 50.0) * 0.12
        if snapshot.source_alignment == "aligned":
            score += 4.0
        elif snapshot.source_alignment == "divergent":
            score -= 6.0
        return max(0.0, min(100.0, score))

    def _compute_alignment(self, sources: Iterable[SourceSentimentSnapshot]) -> str:
        directions = []
        for source in sources:
            label = self._compute_label(source.sentiment_score, source.bullish_pct)
            if label != "unavailable":
                directions.append(label)
        if not directions:
            return "unavailable"
        unique = set(directions)
        if len(unique) == 1:
            return "single_source" if len(directions) == 1 else "aligned"
        if "bullish" in unique and "bearish" in unique:
            return "divergent"
        return "mixed"

    def _compute_label(
        self,
        sentiment_score: Optional[float],
        bullish_pct: Optional[float],
    ) -> str:
        if sentiment_score is not None:
            if sentiment_score >= 0.15:
                return "bullish"
            if sentiment_score <= -0.15:
                return "bearish"
        if bullish_pct is not None:
            if bullish_pct >= 55:
                return "bullish"
            if bullish_pct <= 45:
                return "bearish"
            return "neutral"
        return "unavailable"

    def _chunk(self, items: List[str], size: int) -> Iterable[List[str]]:
        for index in range(0, len(items), size):
            yield items[index : index + size]

    def _average(self, values: List[float]) -> Optional[float]:
        if not values:
            return None
        return sum(values) / len(values)

    def _as_float(self, value) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
