import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from scanners.market_sentiment import MarketSentimentClient, StockSentimentSnapshot


def test_market_sentiment_client_merges_sources_and_scores():
    client = MarketSentimentClient(api_key="test-key")
    rows_by_path = {
        "/reddit/stocks/v1/compare": [
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc.",
                "buzz_score": 61,
                "sentiment_score": 0.28,
                "bullish_pct": 66,
                "mentions": 140,
                "trend": "rising",
            }
        ],
        "/x/stocks/v1/compare": [
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc.",
                "buzz_score": 58,
                "sentiment_score": 0.19,
                "bullish_pct": 61,
                "mentions": 220,
                "trend": "stable",
            }
        ],
        "/news/stocks/v1/compare": [
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc.",
                "buzz_score": 47,
                "sentiment_score": 0.14,
                "bullish_pct": 57,
                "mentions": 31,
                "trend": "stable",
            }
        ],
        "/polymarket/stocks/v1/compare": [
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc.",
                "buzz_score": 55,
                "sentiment_score": 0.22,
                "bullish_pct": 63,
                "trade_count": 95,
                "trend": "rising",
            }
        ],
    }

    def fake_fetch(path, tickers, days):
        return rows_by_path[path]

    client._fetch_compare_rows = fake_fetch

    snapshots = client.get_stock_sentiment(["AAPL"], days=7)

    assert "AAPL" in snapshots
    snapshot = snapshots["AAPL"]
    assert snapshot.company_name == "Apple Inc."
    assert snapshot.coverage == 4
    assert snapshot.source_alignment == "aligned"
    assert snapshot.sentiment_label == "bullish"
    assert snapshot.score_component > 50
    assert round(snapshot.average_buzz, 2) == 55.25


def test_market_sentiment_client_marks_divergence():
    client = MarketSentimentClient(api_key="test-key")

    def fake_fetch(path, tickers, days):
        if "reddit" in path:
            return [{"ticker": "TSLA", "buzz_score": 70, "sentiment_score": 0.4, "bullish_pct": 72, "mentions": 80}]
        if "/x/" in path:
            return [{"ticker": "TSLA", "buzz_score": 68, "sentiment_score": -0.3, "bullish_pct": 31, "mentions": 140}]
        return []

    client._fetch_compare_rows = fake_fetch
    snapshot = client.get_stock_sentiment(["TSLA"])["TSLA"]
    assert snapshot.coverage == 2
    assert snapshot.source_alignment == "divergent"
    assert snapshot.sentiment_label in {"neutral", "bullish", "bearish"}
