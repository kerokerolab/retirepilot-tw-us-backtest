"""
RetirePilot MVP — 台美股 400 大核心標的 20 年歷史日 K、拆併股與除權息自動同步腳本
整合 backend_mvp/universe_400.py (含三級分流：真實上市年 inception_year 與嚴格白名單代理 proxy_ticker)。

執行方式：
  1. 同步全部 400 檔標的：
     python3 backend_mvp/sync_20yr_market_data.py --mode all
  2. 僅同步特定分類 (例如台股百大 ETF 含 00631L)：
     python3 backend_mvp/sync_20yr_market_data.py --category TW_TOP100_ETF
  3. 僅同步指定代號清單：
     python3 backend_mvp/sync_20yr_market_data.py --tickers 00631L.TW 0050.TW 2330.TW VOO QLD
"""

import argparse
import sqlite3
import pathlib
from datetime import datetime
from typing import List, Optional
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

from universe_400 import UNIVERSE_400, MVP_UNIVERSE, MVP_UNIVERSE_EXTENDED

CORE_PRIORITY_TICKERS = [
    "00631L.TW", "00675L.TW", "00663L.TW", "00685L.TW", "00670L.TW", "00680L.TW",
    "0050.TW", "006208.TW", "0056.TW", "00878.TW", "00919.TW", "00929.TW", "00713.TW", "00757.TW", "00679B.TWO",
    "2330.TW", "2317.TW", "2454.TW", "2308.TW", "2881.TW", "2882.TW", "2891.TW", "2412.TW", "6669.TW",
    "VOO", "SPY", "VTI", "QQQ", "VT", "SCHD", "VIG", "SOXX", "TLT", "IEF", "SHY", "GLD", "QLD", "SSO", "TQQQ",
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "BRK-B", "TSM",
]


def init_db(db_path: pathlib.Path, schema_path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    if schema_path.exists():
        conn.executescript(schema_path.read_text(encoding="utf-8"))
    # 相容性自動升級：若舊版 SQLite 資料庫尚未包含三級分流欄位則自動補上
    existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(instruments)").fetchall()]
    if "inception_year" not in existing_cols:
        conn.execute("ALTER TABLE instruments ADD COLUMN inception_year INT DEFAULT 2006")
    if "proxy_ticker" not in existing_cols:
        conn.execute("ALTER TABLE instruments ADD COLUMN proxy_ticker VARCHAR(20) DEFAULT NULL")
    if "proxy_leverage" not in existing_cols:
        conn.execute("ALTER TABLE instruments ADD COLUMN proxy_leverage NUMERIC(5,2) DEFAULT 1.0")
    conn.commit()
    return conn


def seed_400_instruments(conn: sqlite3.Connection):
    """將完整 400 檔台美股標的（含真實上市年與嚴格白名單代理母標的）寫入 instruments 表"""
    now_str = datetime.now().isoformat()
    conn.executemany(
        """
        INSERT OR REPLACE INTO instruments
        (ticker, name_zh, market, category, currency, withholding_tax_rate, inception_year, proxy_ticker, proxy_leverage, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (t, n, m, c, cur, tax, inc_yr, p_tk, p_lev, now_str)
            for (t, n, m, c, cur, tax, inc_yr, p_tk, p_lev) in MVP_UNIVERSE_EXTENDED
        ],
    )
    conn.commit()


def sync_fx_rates(conn: sqlite3.Connection, period: str = "20y"):
    if yf is None:
        return
    fx_hist = yf.Ticker("TWD=X").history(period=period, auto_adjust=False)
    if not fx_hist.empty:
        fx_df = fx_hist[["Close"]].reset_index()
        fx_df.columns = ["rate_date", "usd_twd"]
        fx_df["rate_date"] = pd.to_datetime(fx_df["rate_date"]).dt.strftime("%Y-%m-%d")
        fx_df.to_sql("fx_rates", conn, if_exists="replace", index=False)
        conn.commit()


def sync_tickers_list(
    tickers: List[str],
    db_file: str = "retirepilot_mvp.sqlite",
    period: str = "20y",
    sync_fx: bool = True,
) -> dict:
    if yf is None:
        raise RuntimeError("請先安裝 yfinance：pip install yfinance pandas")

    base_dir = pathlib.Path(__file__).resolve().parent
    db_path = base_dir / db_file
    schema_path = base_dir / "schema.sql"
    conn = init_db(db_path, schema_path)
    seed_400_instruments(conn)

    if sync_fx:
        print("[FX] 正在同步 USD/TWD 20 年歷史每日匯率...")
        sync_fx_rates(conn, period=period)

    meta_map = {t: (n, m, c, cur, tax) for (t, n, m, c, cur, tax) in MVP_UNIVERSE}
    synced_count = 0
    total_rows = 0

    for idx, ticker in enumerate(tickers, 1):
        name_zh = meta_map.get(ticker, (ticker,))[0]
        try:
            tk = yf.Ticker(ticker)
            hist = tk.history(period=period, auto_adjust=False, actions=True)
            if hist.empty and ticker.endswith(".TWO"):
                alt_ticker = ticker.replace(".TWO", ".TW")
                hist = yf.Ticker(alt_ticker).history(period=period, auto_adjust=False, actions=True)
            if hist.empty:
                print(f"  [{idx}/{len(tickers)}] [略過] {ticker} ({name_zh}) 無回傳日線資料")
                continue

            hist = hist.reset_index()
            hist["Date"] = pd.to_datetime(hist["Date"]).dt.strftime("%Y-%m-%d")
            hist["ticker"] = ticker

            # 根據真實抓到的最早交易日更新 inception_year
            min_trade_yr = int(hist["Date"].str.slice(0, 4).min())
            conn.execute(
                "UPDATE instruments SET inception_year = ? WHERE ticker = ?",
                (max(2006, min_trade_yr), ticker),
            )

            price_df = hist[
                ["ticker", "Date", "Open", "High", "Low", "Close", "Adj Close", "Stock Splits", "Volume"]
            ].copy()
            price_df.columns = [
                "ticker",
                "trade_date",
                "open_price",
                "high_price",
                "low_price",
                "close_price",
                "adj_close",
                "split_factor",
                "volume",
            ]
            price_df["split_factor"] = price_df["split_factor"].replace(0, 1.0).fillna(1.0)

            div_rows = hist[hist["Dividends"] > 0][["ticker", "Date", "Dividends"]].copy()
            if not div_rows.empty:
                div_rows.columns = ["ticker", "ex_date", "cash_dividend"]
                div_rows["pay_date"] = div_rows["ex_date"]
                div_rows["stock_dividend"] = 0.0

            # 供應商量綱斷裂 (Scale-Break 壞點) 修復：
            # 1. 0050.TW 自 2014-01-01 起被 Yahoo Finance 供應商錯誤除以 4
            if ticker == "0050.TW":
                m_50 = price_df["trade_date"] >= "2014-01-01"
                for col in ["open_price", "high_price", "low_price", "close_price", "adj_close"]:
                    price_df.loc[m_50, col] *= 4.0
                if not div_rows.empty:
                    m_div = div_rows["ex_date"] >= "2014-01-01"
                    div_rows.loc[m_div, "cash_dividend"] *= 4.0
            # 2. 00631L.TW 2026年 1拆22 分割回溯時，2014年底未調整之尺度跳水
            elif ticker == "00631L.TW":
                ratio_631 = 19.049999 / 0.869090
                m_631 = price_df["trade_date"] < "2015-01-01"
                for col in ["open_price", "high_price", "low_price", "close_price", "adj_close"]:
                    price_df.loc[m_631, col] /= ratio_631

            conn.execute("DELETE FROM daily_prices WHERE ticker = ?", (ticker,))
            price_df.to_sql("daily_prices", conn, if_exists="append", index=False)

            conn.execute("DELETE FROM corporate_dividends WHERE ticker = ?", (ticker,))
            if not div_rows.empty:
                div_rows.to_sql("corporate_dividends", conn, if_exists="append", index=False)

            conn.commit()
            synced_count += 1
            total_rows += len(price_df)
            print(
                f"  [{idx}/{len(tickers)}] [OK] {ticker:10s} {name_zh:14s} (真實起點:{min_trade_yr}) | 日線: {len(price_df):5d} 筆 | 除息: {len(div_rows):3d} 次"
            )
        except Exception as e:
            print(f"  [{idx}/{len(tickers)}] [錯誤] {ticker}: {e}")

    conn.close()
    return {"synced_tickers": synced_count, "total_price_rows_added": total_rows}


def sync_all_data(
    db_file: str = "retirepilot_mvp.sqlite",
    period: str = "20y",
    mode: str = "core",
    category: Optional[str] = None,
    custom_tickers: Optional[List[str]] = None,
):
    if custom_tickers:
        target_tickers = custom_tickers
    elif category:
        target_tickers = [t for (t, _, _, c, _, _) in MVP_UNIVERSE if c == category]
    elif mode == "all":
        target_tickers = [t for (t, _, _, _, _, _) in MVP_UNIVERSE]
    else:
        target_tickers = CORE_PRIORITY_TICKERS

    print(f"開始同步 {len(target_tickers)} 檔台美股 20 年歷史日線與除權息資料...")
    res = sync_tickers_list(target_tickers, db_file=db_file, period=period, sync_fx=True)
    print(f"\n同步完成！共同步 {res['synced_tickers']} 檔標的，寫入 {res['total_price_rows_added']:,} 筆日 K 紀錄。")
    return res


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RetirePilot 400 大台美股 20 年歷史資料同步工具")
    parser.add_argument("--mode", choices=["core", "all"], default="core", help="core=優先同步熱門與正2代表標的; all=同步完整400檔")
    parser.add_argument("--category", choices=["TW_TOP100_CAP", "TW_TOP100_ETF", "US_TOP100_CAP", "US_TOP100_ETF"], help="指定單一分類(100檔)同步")
    parser.add_argument("--tickers", nargs="+", help="指定 1~N 檔股票代號同步 (例: 00631L.TW 0050.TW VOO)")
    args = parser.parse_args()

    sync_all_data(mode=args.mode, category=args.category, custom_tickers=args.tickers)
