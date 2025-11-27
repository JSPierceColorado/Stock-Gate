import os
import json
import time
import logging
from typing import List, Dict, Any, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials

# ==========================
# Config
# ==========================

# Google Sheets
GOOGLE_SA_JSON_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Active-Investing")
DASHBOARD_TAB_NAME = os.getenv("DASHBOARD_TAB_NAME", "Dashboard")
TARGET_CELL = os.getenv("TARGET_CELL", "T3")  # ONLY this cell will be updated

# Alpaca Market Data
ALPACA_KEY_ENV = "ALPACA_API_KEY_ID"
ALPACA_SECRET_ENV = "ALPACA_API_SECRET_KEY"
ALPACA_DATA_BASE_URL = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets")
ALPACA_SYMBOL = os.getenv("ALPACA_SYMBOL", "RSP")

# Moving average window
MA_WINDOW = int(os.getenv("MA_WINDOW", "960"))  # 960-day MA

# Loop interval (seconds) – change if you want it less frequent
REFRESH_INTERVAL_SECONDS = int(os.getenv("REFRESH_INTERVAL_SECONDS", "3600"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("rsp_ma_bot")

# Google API scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ==========================
# Google Sheets Helpers
# ==========================

def get_gspread_client() -> gspread.Client:
    raw_json = os.getenv(GOOGLE_SA_JSON_ENV)
    if not raw_json:
        raise RuntimeError(
            f"Missing Google service account JSON in env var {GOOGLE_SA_JSON_ENV}"
        )

    sa_info = json.loads(raw_json)
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    client = gspread.authorize(creds)
    logger.debug("Initialized gspread client.")
    return client


def get_dashboard_worksheet(client: gspread.Client) -> gspread.Worksheet:
    logger.debug("Opening spreadsheet '%s'...", SHEET_NAME)
    sh = client.open(SHEET_NAME)
    ws = sh.worksheet(DASHBOARD_TAB_NAME)
    logger.debug("Using worksheet '%s'.", DASHBOARD_TAB_NAME)
    return ws


def update_dashboard_cell(ws: gspread.Worksheet, value: str) -> None:
    """
    IMPORTANT: Only update the single target cell (T3 by default).
    """
    logger.info("Updating %s!%s with value '%s'.", DASHBOARD_TAB_NAME, TARGET_CELL, value)
    ws.update(TARGET_CELL, value)


# ==========================
# Alpaca Market Data Helpers
# ==========================

def fetch_rsp_daily_bars(
    symbol: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Fetch 1Day bars for the given symbol from Alpaca's market data API.
    Uses limit=limit and returns the raw list of bars.
    """
    api_key = os.getenv(ALPACA_KEY_ENV)
    api_secret = os.getenv(ALPACA_SECRET_ENV)

    if not api_key or not api_secret:
        raise RuntimeError(
            f"Missing Alpaca API credentials. "
            f"Ensure {ALPACA_KEY_ENV} and {ALPACA_SECRET_ENV} are set."
        )

    url = f"{ALPACA_DATA_BASE_URL}/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": "1Day",
        "limit": limit,
        "adjustment": "all",
    }
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }

    logger.info(
        "Requesting %d daily bars for %s from Alpaca: %s with params %s",
        limit,
        symbol,
        url,
        params,
    )

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    logger.debug("Alpaca response status: %s", resp.status_code)

    try:
        resp.raise_for_status()
    except Exception as e:
        logger.error("Error response from Alpaca: %s", resp.text)
        raise

    data = resp.json()
    bars = data.get("bars", [])

    logger.info("Received %d bars for %s.", len(bars), symbol)

    if not bars:
        logger.warning("No bars returned for %s. Check symbol or permissions.", symbol)
        return []

    # Sort by timestamp just to be safe
    bars = sorted(bars, key=lambda b: b.get("t"))

    # Debug: show first and last bar plus last few closes
    first_bar = bars[0]
    last_bar = bars[-1]
    logger.debug(
        "First bar: t=%s o=%.4f h=%.4f l=%.4f c=%.4f v=%s",
        first_bar.get("t"),
        first_bar.get("o"),
        first_bar.get("h"),
        first_bar.get("l"),
        first_bar.get("c"),
        first_bar.get("v"),
    )
    logger.debug(
        "Last bar: t=%s o=%.4f h=%.4f l=%.4f c=%.4f v=%s",
        last_bar.get("t"),
        last_bar.get("o"),
        last_bar.get("h"),
        last_bar.get("l"),
        last_bar.get("c"),
        last_bar.get("v"),
    )

    logger.debug("Last 5 closes for %s:", symbol)
    for b in bars[-5:]:
        logger.debug("  t=%s c=%.4f", b.get("t"), b.get("c"))

    return bars


# ==========================
# Core Logic
# ==========================

def compute_moving_average(bars: List[Dict[str, Any]], window: int) -> float:
    if not bars:
        raise ValueError("No bars available to compute moving average.")

    closes = [float(b["c"]) for b in bars if "c" in b]

    if len(closes) < window:
        logger.warning(
            "Only %d closes available; requested window is %d. "
            "Will compute MA over available closes.",
            len(closes),
            window,
        )
        window = len(closes)

    relevant = closes[-window:]
    ma_value = sum(relevant) / window

    logger.info(
        "Computed %d-day MA for %s closes: MA=%.4f",
        window,
        ALPACA_SYMBOL,
        ma_value,
    )
    return ma_value


def classify_trend(last_price: float, ma_value: float) -> Tuple[str, float]:
    """
    Classification based on your rules:

    - WEAK:     price > 10% above MA
    - MODERATE: price is above MA but less than or equal to 10% above MA
    - STRONG:   price below MA

    diff_pct is (price - MA) / MA * 100
    """
    diff_pct = (last_price - ma_value) / ma_value * 100.0

    if last_price > ma_value * 1.10:
        label = "WEAK"
    elif last_price >= ma_value:
        label = "MODERATE"
    else:
        label = "STRONG"

    logger.info(
        "Classification for %s: last_price=%.4f, MA=%.4f, diff=%.2f%% => %s",
        ALPACA_SYMBOL,
        last_price,
        ma_value,
        diff_pct,
        label,
    )

    return label, diff_pct


def run_once() -> None:
    """
    Single full cycle:
    - Fetch bars from Alpaca for RSP
    - Compute 960-day MA
    - Classify as WEAK/MODERATE/STRONG
    - Update Dashboard!T3
    """
    logger.info("Starting RSP MA update cycle...")

    # 1) Google Sheets setup
    client = get_gspread_client()
    ws = get_dashboard_worksheet(client)

    # 2) Fetch daily bars
    bars = fetch_rsp_daily_bars(ALPACA_SYMBOL, limit=MA_WINDOW)

    if not bars:
        logger.error("No bars returned for %s; skipping sheet update.", ALPACA_SYMBOL)
        return

    # 3) Compute MA and current price
    ma_value = compute_moving_average(bars, MA_WINDOW)
    last_bar = bars[-1]
    last_price = float(last_bar["c"])
    last_time = last_bar.get("t")

    logger.info(
        "Latest RSP bar: t=%s close=%.4f (used for comparison with 960-day MA).",
        last_time,
        last_price,
    )

    # 4) Classify trend
    label, diff_pct = classify_trend(last_price, ma_value)

    # 5) Update ONLY T3
    # (Optionally read old value for debugging, still only touching T3)
    try:
        previous_value = ws.acell(TARGET_CELL).value
    except Exception:
        previous_value = None

    logger.info(
        "Previous value in %s!%s was: %s",
        DASHBOARD_TAB_NAME,
        TARGET_CELL,
        previous_value,
    )

    update_dashboard_cell(ws, label)

    logger.info(
        "Finished cycle. Dashboard!%s is now '%s' (diff vs MA: %.2f%%).",
        TARGET_CELL,
        label,
        diff_pct,
    )


def main() -> None:
    logger.info(
        "RSP MA bot starting. Sheet='%s', tab='%s', cell='%s', interval=%ss",
        SHEET_NAME,
        DASHBOARD_TAB_NAME,
        TARGET_CELL,
        REFRESH_INTERVAL_SECONDS,
    )

    while True:
        try:
            run_once()
        except Exception as e:
            logger.exception("Unexpected error during update cycle: %s", e)

        logger.info("Sleeping for %d seconds before next run...", REFRESH_INTERVAL_SECONDS)
        time.sleep(REFRESH_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
