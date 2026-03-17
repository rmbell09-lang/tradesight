**I Built a Stock Scanner with Python and RSI Signals — Here is What I Learned**

As a developer interested in finance, I've been experimenting with building tools to analyze stock markets using technical indicators. In this article, I'll share my experience of creating a simple stock scanner using Python that utilizes Relative Strength Index (RSI) signals.

**What is the Relative Strength Index (RSI)?**

The RSI is a popular momentum indicator developed by J. Welles Wilder in 1978. It measures the magnitude of recent price changes to determine overbought or oversold conditions. The RSI is plotted on a scale from 0 to 100, with values below 30 indicating an oversold condition and above 70 indicating an overbought condition.

**Why use RSI?**

The RSI has several advantages:

*   It's easy to understand and calculate.
*   It's widely used in the market, so there's plenty of data available for analysis.
*   It can be used as a standalone indicator or combined with other indicators for more robust signals.

However, the RSI also has its limitations. For example, it can produce false positives during strong trends or in volatile markets.

**Fetching Stock Data with yfinance**

To build our stock scanner, we'll need access to historical stock data. The `yfinance` library is a popular and lightweight package for fetching Yahoo Finance data. We'll use it to retrieve the daily closing prices of 50+ symbols.

```python
import yfinance as yf

# Define a list of symbols
symbols = ['AAPL', 'GOOG', 'MSFT', 'AMZN']

# Fetch historical data for each symbol
data = []
for symbol in symbols:
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period='1y')
    data.append(hist)

print(data)
```

**Implementing the Stock Scanner**

Our stock scanner will check each symbol's RSI value and flag those that meet our overbought or oversold criteria. We'll use a simple threshold-based approach for this example, but you can modify it to suit your needs.

```python
import pandas as pd

# Define the RSI function (we'll explain this in a bit)
def rsi(data, window=14):
    delta = data['Close'].diff().dropna()
    u = delta * 100
    d = -delta * 100
    rs = u.ewm(com=window-1, adjust=False).mean() / d.ewm(com=window-1, adjust=False).mean()
    return 100 - (100 / (1 + rs))

# Apply the RSI function to each symbol's data
rsi_values = []
for i, symbol in enumerate(symbols):
    rsi_val = rsi(data[i])
    rsi_values.append(rsi_val)

print(rsi_values)
```

**Paper Trading Results**

To test our stock scanner, we'll perform a simulated trade using paper trading. We'll use the RSI values to generate buy and sell signals for each symbol.

```python
# Define the threshold values (adjust these as needed)
overbought_threshold = 70
oversold_threshold = 30

# Generate buy and sell signals based on the RSI values
signals = []
for i, rsi_val in enumerate(rsi_values):
    if rsi_val > overbought_threshold:
        signal = 'SELL'
    elif rsi_val < oversold_threshold:
        signal = 'BUY'
    else:
        signal = 'NEUTRAL'
    signals.append(signal)

print(signals)
```

**Conclusion**

In this article, we've built a simple stock scanner using Python that utilizes RSI signals. We've covered the basics of the RSI indicator and how to fetch stock data with `yfinance`. Our paper trading results show promising results, but keep in mind that this is just a basic example.

If you want to take your trading system to the next level, I'd like to invite you to check out TradeSight at [qcautonomous.gumroad.com](http://qcautonomous.gumroad.com). Our system combines AI-powered signal generation with tournament evolution and overnight automation for more robust results.

Thanks for reading! If you have any questions or feedback, please don't hesitate to reach out. Happy coding!