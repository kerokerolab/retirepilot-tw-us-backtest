"""
RetirePilot MVP — FastAPI 輕量前端掛載與 SQLite 真實日資料回測伺服器 (v1.4 Stage 3 完整版)
檔案位置：backend_mvp/app.py
啟動方式：
    pip install -r requirements.txt
    python3 backend_mvp/app.py
啟動後請打開瀏覽器造訪：
    - 互動式 Web App 前端：http://127.0.0.1:8000
    - Swagger 自動 API 文件：http://127.0.0.1:8000/docs
"""

import os
import gzip
import shutil
import json
import sqlite3
import pathlib
from typing import List, Dict, Optional
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from universe_400 import UNIVERSE_400, MVP_UNIVERSE, STRICT_PROXY_WHITELIST, get_frontend_stock_db_json
from backtest_and_retire_engine import (
    RetirementInput,
    MonthlyBacktestInput,
    LoanSimInput,
    simulate_lifecycle_and_withdrawals,
    simulate_monthly_share_based,
    simulate_loan_investment,
    GOV_SALARY_MEDIANS,
)
from sync_20yr_market_data import init_db, seed_400_instruments, sync_all_data, sync_tickers_list

BASE_DIR = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DB_PATH = BASE_DIR / "retirepilot_mvp.sqlite"
SEED_GZ_PATH = BASE_DIR / "retirepilot_cloud_seed.sqlite.gz"
SCHEMA_PATH = BASE_DIR / "schema.sql"
HTML_PATH = PROJECT_ROOT / "prototype" / "retire_backtest_mvp.html"

app = FastAPI(
    title="RetirePilot 台美股 20 年回測、法定雙軌退休金與貸款自償模擬 API",
    description="支援 400 大台美股、任意區間月K真實股數回測、勞保/勞退新制精算、退休目標逆向反推與貸款投入股市自償模擬",
    version="1.4.0",
)


def ensure_database_initialized():
    # 若雲端環境尚無 retirepilot_mvp.sqlite，優先從隨附之 5.1MB 雲端種子壓縮檔自動解壓縮還原
    if (not DB_PATH.exists() or DB_PATH.stat().st_size < 100000) and SEED_GZ_PATH.exists():
        print(f"📦 偵測到雲端初次啟動，正在從 {SEED_GZ_PATH.name} 解壓縮 20 年歷史月 K 資料庫...")
        with gzip.open(SEED_GZ_PATH, "rb") as f_in, open(DB_PATH, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        print("✅ 雲端歷史資料庫解壓縮完成！")

    conn = init_db(DB_PATH, SCHEMA_PATH)
    seed_400_instruments(conn)
    conn.close()


ensure_database_initialized()


class PortfolioItem(BaseModel):
    ticker: str
    weight: float


class BacktestRequest(BaseModel):
    portfolio: List[PortfolioItem]
    invest_mode: str = "both"  # "both", "lump_only", "dca_only"
    lump_sum: float = 300000.0
    monthly_dca: float = 15000.0
    dca_growth_rate: float = 0.0
    drip: bool = True
    apply_us_tax: bool = True
    rebalance_yearly: bool = True
    backfill_mode: str = "strict_truncate"  # "strict_truncate", "whitelist_proxy", "dynamic_parking"
    start_ym: str = "2006-01"
    end_ym: str = "2025-12"


class SyncTickersRequest(BaseModel):
    tickers: Optional[List[str]] = None
    mode: str = "core"


class RetireSimRequest(BaseModel):
    current_age: int = 32
    retire_age: int = 60
    max_age: int = 90
    monthly_salary: float = 65000.0
    initial_capital: float = 500000.0
    saving_rate: float = 0.25
    salary_growth_rate: float = 0.03
    salary_linked_dca: bool = True
    salary_mode: str = "fixed_growth"
    custom_salary_map: Optional[Dict[int, float]] = None
    expected_annual_return: float = 0.108
    expected_dividend_yield: float = 0.032
    retire_monthly_spend_today: float = 50000.0
    inflation_rate: float = 0.022
    market_scenario: str = "historical_cycle"
    withdraw_timing: str = "annual_boy"
    withdraw_rule: str = "rule_4pct"
    median_salary_ratio: float = 1.0
    extra_expenses: Optional[List[Dict]] = None
    labor_ins_enabled: bool = True
    labor_ins_mode: str = "annuity"
    labor_ins_start_work_age: int = 24
    labor_ins_claim_age: int = 65
    labor_ins_avg_wage: float = 45800.0
    labor_pen_enabled: bool = True
    labor_pen_mode: str = "monthly"
    labor_pen_emp_rate: float = 0.06
    labor_pen_self_rate: float = 0.0
    labor_pen_init_balance: float = 300000.0
    labor_pen_return_rate: float = 0.04
    loan_inject_enabled: bool = False
    loan_inject_age: int = 52
    loan_inject_amount: float = 0.0


class LoanSimRequest(BaseModel):
    loan_amount: float = 1500000.0
    loan_term_months: int = 84
    annual_interest_rate: float = 0.05
    grace_period_months: int = 0
    repay_schedule: str = "amortized"
    repay_source: str = "self_buffer_refi"
    equity_allocation_pct: float = 0.80
    cash_pool_annual_yield: float = 0.0
    refi_enabled: bool = True
    refi_interval_months: int = 48
    total_sim_months: int = 240
    return_mode: str = "fixed_cagr"  # "fixed_cagr" or "historical_kline"
    expected_annual_return: float = 0.14
    historical_ticker: str = "00631L.TW"
    start_ym: str = "2006-01"
    end_ym: str = "2025-12"
    backfill_mode: str = "whitelist_proxy"
    apply_us_tax: bool = True
    post_payoff_dca: bool = True


def load_monthly_data_from_sqlite(
    conn: sqlite3.Connection,
    ticker: str,
    apply_us_tax: bool,
    backfill_mode: str = "strict_truncate",
) -> List[Dict]:
    """
    從 SQLite 讀取指定股票的每日價格與除息，按 YYYY-MM 分組萃取出月 K 數據
    """
    price_df = pd.read_sql_query(
        """
        SELECT trade_date, close_price, adj_close
        FROM daily_prices
        WHERE ticker = ? AND trade_date >= '2006-01-01'
        ORDER BY trade_date ASC
        """,
        conn,
        params=(ticker,),
    )
    result = []
    if not price_df.empty and len(price_df) >= 20:
        meta_row = conn.execute(
            "SELECT market, currency, withholding_tax_rate FROM instruments WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        market = meta_row[0] if meta_row else ("US" if not (ticker.endswith(".TW") or ticker.endswith(".TWO")) else "TW")
        tax_rate = float(meta_row[2]) if (meta_row and apply_us_tax) else (0.30 if market == "US" and apply_us_tax else 0.0)

        # 供應商量綱斷裂（Scale-Break 壞點）防禦層
        if ticker == "0050.TW":
            p_2013 = price_df[price_df["trade_date"].str.startswith("2013")]["close_price"].mean()
            p_2014 = price_df[price_df["trade_date"].str.startswith("2014")]["close_price"].mean()
            if p_2013 > 40 and p_2014 < 25:
                mask_2014 = price_df["trade_date"] >= "2014-01-01"
                price_df.loc[mask_2014, "close_price"] *= 4.0
                price_df.loc[mask_2014, "adj_close"] *= 4.0
        elif ticker == "00631L.TW":
            p_2014 = price_df[price_df["trade_date"].str.startswith("2014")]["close_price"].mean()
            p_2015 = price_df[price_df["trade_date"].str.startswith("2015")]["close_price"].mean()
            if p_2014 > 15 and p_2015 < 2:
                ratio = p_2014 / p_2015
                mask_pre2015 = price_df["trade_date"] < "2015-01-01"
                price_df.loc[mask_pre2015, "close_price"] /= ratio
                price_df.loc[mask_pre2015, "adj_close"] /= ratio

        price_df["ym"] = price_df["trade_date"].str.slice(0, 7)
        monthly_grp = price_df.groupby("ym").agg(
            start_price=("close_price", "first"),
            end_price=("close_price", "last"),
        ).reset_index()

        div_df = pd.read_sql_query(
            """
            SELECT ex_date, cash_dividend
            FROM corporate_dividends
            WHERE ticker = ? AND ex_date >= '2006-01-01'
            """,
            conn,
            params=(ticker,),
        )
        div_map = {}
        if not div_df.empty:
            div_df["ym"] = div_df["ex_date"].str.slice(0, 7)
            div_map = div_df.groupby("ym")["cash_dividend"].sum().to_dict()

        prev_end_p = None
        for _, row in monthly_grp.iterrows():
            ym = row["ym"]
            p_start = float(prev_end_p) if prev_end_p is not None else float(row["start_price"])
            p_end = float(row["end_price"])
            prev_end_p = p_end
            d_cash = float(div_map.get(ym, 0.0))
            result.append({
                "year_month": ym,
                "year": int(ym.split("-")[0]),
                "month": int(ym.split("-")[1]),
                "start_price": p_start,
                "end_price": p_end,
                "div_cash": d_cash,
                "tax_rate": tax_rate,
            })

    if backfill_mode == "whitelist_proxy" and ticker in STRICT_PROXY_WHITELIST:
        proxy_ticker, proxy_lev = STRICT_PROXY_WHITELIST[ticker]
        proxy_monthly = load_monthly_data_from_sqlite(conn, proxy_ticker, apply_us_tax, "strict_truncate")
        if proxy_monthly:
            earliest_actual_ym = result[0]["year_month"] if result else "9999-99"
            missing_proxy_months = [m for m in proxy_monthly if m["year_month"] < earliest_actual_ym]
            if missing_proxy_months:
                anchor_price = result[0]["start_price"] if result else 100.0
                synth_months = []
                curr_p_end = anchor_price
                for pm in reversed(missing_proxy_months):
                    p_div_net = pm.get("div_cash", 0.0) * (1.0 - pm.get("tax_rate", 0.0))
                    p_ret = ((pm["end_price"] + p_div_net) / pm["start_price"]) - 1.0 if pm["start_price"] > 0 else 0.0
                    synth_ret = max(-0.95, p_ret * proxy_lev)
                    p_start = curr_p_end / (1.0 + synth_ret) if (1.0 + synth_ret) > 0 else curr_p_end
                    synth_months.append({
                        "year_month": pm["year_month"],
                        "year": pm["year"],
                        "month": pm["month"],
                        "start_price": round(p_start, 4),
                        "end_price": round(curr_p_end, 4),
                        "div_cash": 0.0,
                        "tax_rate": 0.0,
                    })
                    curr_p_end = p_start
                synth_months.reverse()
                result = synth_months + result

    return result


def load_annual_series_from_sqlite(conn: sqlite3.Connection, ticker: str, apply_us_tax: bool) -> Optional[Dict]:
    """保留年度序列輔助函式，支援傳統模式與快速概算"""
    price_df = pd.read_sql_query(
        """
        SELECT trade_date, close_price, adj_close
        FROM daily_prices
        WHERE ticker = ? AND trade_date >= '2006-01-01'
        ORDER BY trade_date ASC
        """,
        conn,
        params=(ticker,),
    )
    if price_df.empty or len(price_df) < 30:
        return None

    meta_row = conn.execute(
        "SELECT market, currency, withholding_tax_rate, inception_year FROM instruments WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    market = meta_row[0] if meta_row else ("US" if not (ticker.endswith(".TW") or ticker.endswith(".TWO")) else "TW")
    tax_rate = float(meta_row[2]) if (meta_row and apply_us_tax) else (0.30 if market == "US" and apply_us_tax else 0.0)

    price_df["year"] = pd.to_datetime(price_df["trade_date"]).dt.year
    actual_inception_year = max(2006, int(price_df["year"].min()))

    yearly_close = price_df.groupby("year").agg(
        first_close=("close_price", "first"),
        last_close=("close_price", "last"),
    )

    div_df = pd.read_sql_query(
        "SELECT ex_date, cash_dividend FROM corporate_dividends WHERE ticker = ? AND ex_date >= '2006-01-01'",
        conn,
        params=(ticker,),
    )
    if not div_df.empty:
        div_df["year"] = pd.to_datetime(div_df["ex_date"]).dt.year
        yearly_div = div_df.groupby("year")["cash_dividend"].sum()
    else:
        yearly_div = pd.Series(dtype=float)

    years = list(range(2006, 2026))
    price_rets, div_yields = [], []
    for y in years:
        if y in yearly_close.index and y >= actual_inception_year:
            row = yearly_close.loc[y]
            p_ret = (row["last_close"] / row["first_close"]) - 1.0 if row["first_close"] > 0 else 0.0
            d_cash = float(yearly_div.get(y, 0.0)) * (1.0 - tax_rate)
            d_yld = (d_cash / row["first_close"]) if row["first_close"] > 0 else 0.0
            price_rets.append(float(p_ret))
            div_yields.append(float(d_yld))
        else:
            price_rets.append(None)
            div_yields.append(None)

    return {
        "years": years,
        "actual_inception_year": actual_inception_year,
        "price_rets": price_rets,
        "div_yields": div_yields,
    }


@app.get("/api/universe")
def get_universe_400():
    items = get_frontend_stock_db_json()
    return {"count": len(items), "items": items}


@app.get("/api/db-status")
def get_db_status():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    inst_cnt = cur.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
    synced_tickers_cnt = cur.execute("SELECT COUNT(DISTINCT ticker) FROM daily_prices").fetchone()[0]
    price_cnt = cur.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
    div_cnt = cur.execute("SELECT COUNT(*) FROM corporate_dividends").fetchone()[0]
    fx_cnt = cur.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0]
    conn.close()
    return {
        "status": "ok",
        "database_file": str(DB_PATH.name),
        "instruments_count": inst_cnt,
        "synced_tickers_count": synced_tickers_cnt,
        "daily_prices_count": price_cnt,
        "dividends_count": div_cnt,
        "fx_rates_count": fx_cnt,
        "has_synced_daily_data": price_cnt > 0,
    }


@app.post("/api/sync")
def trigger_yfinance_sync(req: Optional[SyncTickersRequest] = None):
    try:
        if req and req.tickers:
            conn = sqlite3.connect(str(DB_PATH))
            extra_proxies = set()
            for tk in req.tickers:
                row = conn.execute("SELECT proxy_ticker FROM instruments WHERE ticker = ?", (tk,)).fetchone()
                if row and row[0]:
                    extra_proxies.add(row[0])
            conn.close()
            combined = list(dict.fromkeys(list(req.tickers) + list(extra_proxies)))
            sync_tickers_list(combined, db_file=str(DB_PATH.name), period="20y", sync_fx=True)
        else:
            mode = req.mode if req else "core"
            sync_all_data(db_file=str(DB_PATH.name), period="20y", mode=mode)
        return get_db_status()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同步失敗：{str(e)}")


@app.post("/api/backtest-monthly")
def api_backtest_monthly(req: BacktestRequest):
    """
    Task 1.3: 呼叫月 K 級別真實股數累積回測引擎（支援任意起迄年月 start_ym ~ end_ym）
    """
    conn = sqlite3.connect(str(DB_PATH))
    monthly_data_by_ticker = {}
    weights = {}
    total_w = sum(it.weight for it in req.portfolio) or 1.0

    start_ym = req.start_ym or "2006-01"
    end_ym = req.end_ym or "2025-12"
    if start_ym > end_ym:
        start_ym, end_ym = end_ym, start_ym

    for it in req.portfolio:
        weights[it.ticker] = it.weight / total_w
        m_list = load_monthly_data_from_sqlite(conn, it.ticker, req.apply_us_tax, req.backfill_mode)
        if m_list:
            filtered = [m for m in m_list if start_ym <= m["year_month"] <= end_ym]
            if filtered:
                monthly_data_by_ticker[it.ticker] = filtered

    conn.close()

    if len(monthly_data_by_ticker) < len(req.portfolio):
        return {
            "has_monthly_data": False,
            "message": "所選區間內部分或全部標的无 SQLite 日 K 資料（可能尚未掛牌上市或未同步）。",
        }

    actual_lump = req.lump_sum if req.invest_mode != "dca_only" else 0.0
    actual_dca = req.monthly_dca if req.invest_mode != "lump_only" else 0.0

    params = MonthlyBacktestInput(
        lump_sum=actual_lump,
        monthly_dca=actual_dca,
        dca_growth_rate=req.dca_growth_rate,
        drip=req.drip,
        apply_us_tax=req.apply_us_tax,
        rebalance_yearly=req.rebalance_yearly,
    )

    port_res = simulate_monthly_share_based(monthly_data_by_ticker, weights, params)
    if not port_res:
        return {
            "has_monthly_data": False,
            "message": "所選區間內無共同有效交易月份。",
        }

    common_start = port_res["start_month"]
    common_end = port_res["end_month"]

    indiv_res = {}
    for it in req.portfolio:
        aligned_list = [
            m for m in monthly_data_by_ticker[it.ticker]
            if common_start <= m["year_month"] <= common_end
        ]
        s_res = simulate_monthly_share_based(
            {it.ticker: aligned_list},
            {it.ticker: 1.0},
            params,
        )
        indiv_res[it.ticker] = s_res

    return {
        "has_monthly_data": True,
        "portfolio_result": port_res,
        "individual_results": indiv_res,
    }


@app.post("/api/backtest")
def api_portfolio_backtest(req: BacktestRequest):
    conn = sqlite3.connect(str(DB_PATH))
    results_per_ticker = {}
    tickers_to_load = set(item.ticker for item in req.portfolio)
    for tk in list(tickers_to_load):
        row = conn.execute("SELECT proxy_ticker FROM instruments WHERE ticker = ?", (tk,)).fetchone()
        if row and row[0]:
            tickers_to_load.add(row[0])

    for tk in tickers_to_load:
        series = load_annual_series_from_sqlite(conn, tk, req.apply_us_tax)
        if series is not None:
            results_per_ticker[tk] = series
    conn.close()
    return {
        "has_sqlite_series": len(results_per_ticker) > 0,
        "sqlite_series": results_per_ticker,
    }


@app.post("/api/retire-sim")
def api_retire_simulate(req: RetireSimRequest):
    us_cycle = [0.15, 0.05, -0.37, 0.26, 0.15, 0.02, 0.16, 0.32, 0.13, 0.01,
                0.12, 0.21, -0.04, 0.31, 0.18, 0.28, -0.18, 0.26, 0.25, 0.16]
    mean_c = sum(us_cycle) / len(us_cycle)
    total_yrs = max(1, req.max_age - req.current_age + 1)
    retire_offset = max(1, req.retire_age - req.current_age)
    custom_rets = []
    for idx in range(total_yrs):
        if req.market_scenario == "steady_avg":
            r = req.expected_annual_return
        else:
            dev = (us_cycle[idx % len(us_cycle)] - mean_c) * 0.75
            r = req.expected_annual_return + dev
            if req.market_scenario == "retire_crash":
                if idx == retire_offset:
                    r = -0.22
                elif idx == retire_offset + 1:
                    r = -0.14
                elif idx == retire_offset + 2:
                    r = 0.18
        custom_rets.append(r)

    inp = RetirementInput(
        current_age=req.current_age,
        retire_age=req.retire_age,
        max_age=req.max_age,
        monthly_salary=req.monthly_salary,
        initial_capital=req.initial_capital,
        saving_rate=req.saving_rate,
        salary_growth_rate=req.salary_growth_rate,
        salary_linked_dca=req.salary_linked_dca,
        salary_mode=req.salary_mode,
        custom_salary_map=req.custom_salary_map,
        expected_annual_return=req.expected_annual_return,
        expected_dividend_yield=req.expected_dividend_yield,
        retire_monthly_spend_today=req.retire_monthly_spend_today,
        inflation_rate=req.inflation_rate,
        withdraw_timing=req.withdraw_timing,
        withdraw_rule=req.withdraw_rule,
        median_salary_ratio=req.median_salary_ratio,
        extra_expenses=req.extra_expenses,
        labor_ins_enabled=req.labor_ins_enabled,
        labor_ins_mode=req.labor_ins_mode,
        labor_ins_start_work_age=req.labor_ins_start_work_age,
        labor_ins_claim_age=req.labor_ins_claim_age,
        labor_ins_avg_wage=req.labor_ins_avg_wage,
        labor_pen_enabled=req.labor_pen_enabled,
        labor_pen_mode=req.labor_pen_mode,
        labor_pen_emp_rate=req.labor_pen_emp_rate,
        labor_pen_self_rate=req.labor_pen_self_rate,
        labor_pen_init_balance=req.labor_pen_init_balance,
        labor_pen_return_rate=req.labor_pen_return_rate,
        loan_inject_enabled=req.loan_inject_enabled,
        loan_inject_age=req.loan_inject_age,
        loan_inject_amount=req.loan_inject_amount,
    )
    return simulate_lifecycle_and_withdrawals(inp, custom_market_returns=custom_rets)


@app.post("/api/loan-sim")
def api_loan_simulate(req: LoanSimRequest):
    """
    Task 3.3: 貸款投入股市與自償模擬 API（支援固定年化報酬與真實 SQLite 歷史月 K 回測）
    """
    hist_monthly = None
    if req.return_mode == "historical_kline":
        conn = sqlite3.connect(str(DB_PATH))
        raw_m = load_monthly_data_from_sqlite(conn, req.historical_ticker, req.apply_us_tax, req.backfill_mode)
        conn.close()
        start_ym = req.start_ym or "2006-01"
        end_ym = req.end_ym or "2025-12"
        if start_ym > end_ym:
            start_ym, end_ym = end_ym, start_ym
        filtered = [m for m in raw_m if start_ym <= m["year_month"] <= end_ym]
        if filtered:
            hist_monthly = []
            for idx, m in enumerate(filtered):
                p_start = filtered[idx - 1]["end_price"] if (idx > 0 and filtered[idx - 1]["end_price"] > 0) else m["start_price"]
                p_end = m["end_price"]
                d_net = m["div_cash"] * (1.0 - (m["tax_rate"] if req.apply_us_tax else 0.0))
                tot_ret = ((p_end + d_net) / p_start - 1.0) if p_start > 0 else 0.0
                hist_monthly.append({
                    "year_month": m["year_month"],
                    "total_return": tot_ret,
                })

    inp = LoanSimInput(
        loan_amount=req.loan_amount,
        loan_term_months=req.loan_term_months,
        annual_interest_rate=req.annual_interest_rate,
        grace_period_months=req.grace_period_months,
        repay_schedule=req.repay_schedule,
        repay_source=req.repay_source,
        equity_allocation_pct=req.equity_allocation_pct,
        cash_pool_annual_yield=req.cash_pool_annual_yield,
        refi_enabled=req.refi_enabled,
        refi_interval_months=req.refi_interval_months,
        total_sim_months=req.total_sim_months,
        expected_annual_return=req.expected_annual_return,
        post_payoff_dca=req.post_payoff_dca,
    )
    res = simulate_loan_investment(inp, historical_monthly_returns=hist_monthly)
    res["used_historical_sqlite"] = hist_monthly is not None
    return res


@app.get("/api/salary-medians")
def get_gov_salary_medians():
    return GOV_SALARY_MEDIANS


def build_frontend_bridge_script() -> str:
    stock_db_json_str = json.dumps(get_frontend_stock_db_json(), ensure_ascii=False)
    return f"""
<script>
(function() {{
  const FULL_400_DB = {stock_db_json_str};
  if (typeof STOCK_DB !== 'undefined' && Array.isArray(STOCK_DB)) {{
    STOCK_DB.length = 0;
    FULL_400_DB.forEach(item => STOCK_DB.push(item));
  }}

  const topbar = document.querySelector('.topbar');
  if (topbar) {{
    const statusBox = document.createElement('div');
    statusBox.style.cssText = 'display:flex; align-items:center; gap:8px; background:rgba(15,23,42,0.58); padding:6px 12px; border-radius:8px; font-size:12px; flex-wrap:wrap;';
    statusBox.innerHTML = `
      <span id="db-live-indicator" style="color:#34d399; font-weight:700;">🟢 月K股數累積回測已就緒</span>
      <span id="db-row-stats" style="color:#cbd5e1;">檢查 SQLite 中...</span>
      <button id="btn-sync-sel" onclick="window.syncSelectedNow()" style="background:#3b82f6; color:#fff; border:none; border-radius:6px; padding:4px 9px; font-size:11.5px; font-weight:700; cursor:pointer;" title="下載自選組合與其白名單母標的之真實 20 年日 K">
        ⚡ 同步自選組合日K
      </button>
      <button id="btn-sync-yf" onclick="window.syncYFinanceNow()" style="background:#10b981; color:#fff; border:none; border-radius:6px; padding:4px 9px; font-size:11.5px; font-weight:700; cursor:pointer;" title="批次下載核心熱門與正2代表標的 20 年日 K">
        🔄 批次同步核心日K
      </button>
      <a href="/docs" target="_blank" style="color:#93c5fd; text-decoration:underline; font-weight:600;">API文件</a>
    `;
    topbar.appendChild(statusBox);
  }}

  const _origGetRaw20YrBaseSeries = window.getRaw20YrBaseSeries;
  window.getRaw20YrBaseSeries = function(stock, useUSTax) {{
    const fallback = _origGetRaw20YrBaseSeries(stock, useUSTax);
    const cached = window._SQLITE_SERIES_CACHE && window._SQLITE_SERIES_CACHE[stock.ticker];
    if (!cached) return fallback;
    if (cached.actual_inception_year && cached.actual_inception_year > (stock.inceptionYear || 2006)) {{
      stock.inceptionYear = cached.actual_inception_year;
    }}
    return {{
      priceRets: cached.price_rets.map((v, i) => (v !== null && v !== undefined) ? v : fallback.priceRets[i]),
      divYields: cached.div_yields.map((v, i) => (v !== null && v !== undefined) ? v : fallback.divYields[i])
    }};
  }};

  async function refreshDBStatus() {{
    try {{
      const res = await fetch('/api/db-status');
      const data = await res.json();
      const statsEl = document.getElementById('db-row-stats');
      if (statsEl) {{
        statsEl.innerText = data.has_synced_daily_data
          ? `SQLite 已同步 ${{data.synced_tickers_count}}/${{data.instruments_count}} 檔 | 日K: ${{data.daily_prices_count.toLocaleString()}} 筆`
          : `SQLite 標的庫: ${{data.instruments_count}} 檔 (點擊右側按鈕可抓真實日K)`;
      }}
      if (data.has_synced_daily_data) {{
        await loadSQLiteSeriesIntoFrontend();
      }}
    }} catch (err) {{
      console.warn('FastAPI status check error:', err);
    }}
  }}

  async function loadSQLiteSeriesIntoFrontend() {{
    if (typeof selectedPortfolio === 'undefined') return;
    const investMode = document.getElementById('bt-investMode')?.value || 'both';
    const payload = {{
      portfolio: selectedPortfolio.map(s => ({{ ticker: s.ticker, weight: s.weight }})),
      invest_mode: investMode,
      lump_sum: parseFloat(document.getElementById('bt-lumpSum')?.value || 300000),
      monthly_dca: parseFloat(document.getElementById('bt-monthlyDCA')?.value || 15000),
      dca_growth_rate: (parseFloat(document.getElementById('bt-dcaGrowth')?.value || 0)) / 100,
      drip: document.getElementById('bt-drip')?.value === 'drip',
      apply_us_tax: document.getElementById('bt-ustax')?.value === 'yes',
      rebalance_yearly: true,
      backfill_mode: document.getElementById('bt-backfillMode')?.value || 'strict_truncate',
      start_ym: document.getElementById('bt-startYM')?.value || '2006-01',
      end_ym: document.getElementById('bt-endYM')?.value || '2025-12'
    }};

    try {{
      const mRes = await fetch('/api/backtest-monthly', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }});
      const mData = await mRes.json();
      if (mData.has_monthly_data) {{
        window._MONTHLY_BACKTEST_DATA = mData;
      }}
    }} catch(e) {{}}

    const res = await fetch('/api/backtest', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload)
    }});
    const data = await res.json();
    if (data.has_sqlite_series) {{
      window._SQLITE_SERIES_CACHE = Object.assign(window._SQLITE_SERIES_CACHE || {{}}, data.sqlite_series);
      if (typeof runBacktest === 'function') runBacktest();
    }}
  }}

  window.syncSelectedNow = async function() {{
    const btn = document.getElementById('btn-sync-sel');
    if (!btn || typeof selectedPortfolio === 'undefined') return;
    btn.disabled = true;
    const tickers = selectedPortfolio.map(p => p.ticker);
    btn.innerText = `⏳ 同步 ${{tickers.length}} 檔日K中...`;
    try {{
      const res = await fetch('/api/sync', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ tickers: tickers, mode: 'custom' }})
      }});
      if (!res.ok) throw new Error(await res.text());
      await refreshDBStatus();
      btn.innerText = '✅ 自選日K已同步';
      setTimeout(() => {{ btn.innerText = '⚡ 同步自選組合日K'; btn.disabled = false; }}, 2500);
    }} catch (e) {{
      alert('同步發生錯誤：' + e.message);
      btn.innerText = '⚡ 重試同步自選';
      btn.disabled = false;
    }}
  }};

  window.syncYFinanceNow = async function() {{
    const btn = document.getElementById('btn-sync-yf');
    if (!btn) return;
    btn.disabled = true;
    btn.innerText = '⏳ 正在從 yfinance 下載核心與正2日K...';
    try {{
      const res = await fetch('/api/sync', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ mode: 'core' }})
      }});
      if (!res.ok) throw new Error(await res.text());
      await refreshDBStatus();
      btn.innerText = '✅ 已載入 SQLite 真實日K';
      setTimeout(() => {{ btn.innerText = '🔄 批次同步核心日K'; btn.disabled = false; }}, 3000);
    }} catch (e) {{
      alert('同步發生錯誤：' + e.message);
      btn.innerText = '🔄 重試批次同步';
      btn.disabled = false;
    }}
  }};

  window.addEventListener('DOMContentLoaded', () => {{
    if (typeof renderStockSelector === 'function') renderStockSelector();
    if (typeof populateLoanTickerSelect === 'function') populateLoanTickerSelect();
    if (typeof runBacktest === 'function') runBacktest();
    refreshDBStatus();
  }});
  setTimeout(() => {{
    if (typeof renderStockSelector === 'function') renderStockSelector();
    if (typeof populateLoanTickerSelect === 'function') populateLoanTickerSelect();
    refreshDBStatus();
  }}, 250);
}})();
</script>
"""


@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    if not HTML_PATH.exists():
        return HTMLResponse("<h3>找不到前端檔案 prototype/retire_backtest_mvp.html</h3>", status_code=404)
    raw_html = HTML_PATH.read_text(encoding="utf-8")
    bridged_html = raw_html.replace("</body>", build_frontend_bridge_script() + "\n</body>")
    return HTMLResponse(bridged_html)


if __name__ == "__main__":
    print("====================================================")
    print("🚀 RetirePilot MVP 伺服器啟動中 (v1.4 Stage 3 完整版)...")
    print("👉 請打開瀏覽器造訪：http://127.0.0.1:8000")
    print("👉 Swagger API 文件：http://127.0.0.1:8000/docs")
    print("====================================================")
    uvicorn.run(app, host="127.0.0.1", port=8000)
