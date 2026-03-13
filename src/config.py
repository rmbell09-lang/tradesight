"""TradeSight configuration with keychain integration."""
import os
import logging
from pathlib import Path

# Import keychain utilities for secure API key management
try:
    from .utils.keychain import (
        get_alpaca_api_key, get_alpaca_secret_key,
        get_polygon_api_key, get_yahoo_api_key, get_openai_api_key
    )
    KEYCHAIN_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Keychain utilities not available: {e}")
    KEYCHAIN_AVAILABLE = False

# Set up logging
logger = logging.getLogger(__name__)

# Base directories
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config" 
LOGS_DIR = BASE_DIR / "logs"

# Ensure directories exist
for directory in [DATA_DIR, CONFIG_DIR, LOGS_DIR]:
    directory.mkdir(exist_ok=True, parents=True)

# API Keys - Use keychain with fallback to environment variables
if KEYCHAIN_AVAILABLE:
    ALPACA_API_KEY = get_alpaca_api_key()
    ALPACA_SECRET_KEY = get_alpaca_secret_key()
    POLYGON_API_KEY = get_polygon_api_key()
    YAHOO_API_KEY = get_yahoo_api_key()
    OPENAI_API_KEY = get_openai_api_key()
    logger.info("Using keychain for API key management")
else:
    # Fallback to environment variables
    ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "") 
    POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")
    YAHOO_API_KEY = os.environ.get("YAHOO_API_KEY", "")
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    logger.warning("Using environment variables for API keys - keychain not available")

# API Configuration
ALPACA_BASE_URL_PAPER = "https://paper-api.alpaca.markets"
ALPACA_BASE_URL_LIVE = "https://api.alpaca.markets"
POLYGON_BASE_URL = "https://api.polygon.io"

# Trading Configuration
USE_PAPER_TRADING = True  # Always start with paper trading for safety
MAX_POSITION_SIZE = 0.10  # 10% of portfolio per position
MAX_DAILY_TRADES = 10
STOP_LOSS_PERCENTAGE = 0.05  # 5% stop loss
TAKE_PROFIT_PERCENTAGE = 0.10  # 10% take profit

# Scanner Configuration
SCAN_INTERVAL_SECONDS = 300  # 5 minutes
MAX_CONCURRENT_SCANS = 3

# Database Configuration  
DATABASE_URL = f"sqlite:///{DATA_DIR / 'tradesight.db'}"

# Keychain management utilities (if available)
if KEYCHAIN_AVAILABLE:
    def get_api_key_status():
        """Get status of all API keys for debugging."""
        keys = {
            "Alpaca-API": bool(ALPACA_API_KEY),
            "Alpaca-Secret": bool(ALPACA_SECRET_KEY),
            "Polygon": bool(POLYGON_API_KEY),
            "Yahoo": bool(YAHOO_API_KEY),
            "OpenAI": bool(OPENAI_API_KEY),
        }
        return keys
    
    def refresh_api_keys():
        """Refresh API keys from keychain - useful for key rotation."""
        global ALPACA_API_KEY, ALPACA_SECRET_KEY, POLYGON_API_KEY, YAHOO_API_KEY, OPENAI_API_KEY
        
        ALPACA_API_KEY = get_alpaca_api_key()
        ALPACA_SECRET_KEY = get_alpaca_secret_key()
        POLYGON_API_KEY = get_polygon_api_key()
        YAHOO_API_KEY = get_yahoo_api_key()
        OPENAI_API_KEY = get_openai_api_key()
        
        logger.info("Refreshed API keys from keychain")
        return get_api_key_status()
        
else:
    def get_api_key_status():
        """Get status of all API keys for debugging."""
        keys = {
            "Alpaca-API": bool(ALPACA_API_KEY),
            "Alpaca-Secret": bool(ALPACA_SECRET_KEY), 
            "Polygon": bool(POLYGON_API_KEY),
            "Yahoo": bool(YAHOO_API_KEY),
            "OpenAI": bool(OPENAI_API_KEY),
        }
        return keys
    
    def refresh_api_keys():
        """Keychain not available - keys cannot be refreshed."""
        logger.warning("Keychain not available - cannot refresh API keys")
        return get_api_key_status()

# Validate critical configuration
if not ALPACA_API_KEY:
    logger.warning("ALPACA_API_KEY not set - Alpaca integration will use demo mode")

# Log startup status
logger.info(f"TradeSight config loaded - Keychain: {KEYCHAIN_AVAILABLE}, API keys: {sum(get_api_key_status().values())}/5")
