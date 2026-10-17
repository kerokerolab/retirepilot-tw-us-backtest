"""
RetirePilot MVP — 核心向量化回測、法定雙軌退休金、逆向目標反推與貸款自償模擬引擎 (Python 模組)
功能包含：
1. 1 ~ N 支台美股權重配置、單筆投入 (Lump Sum)、定期定額 (DCA)、隨薪資成長動態加碼回測
2. 股息再投入 (DRIP) vs. 股息領現、美股 30% 股息預扣稅計算、年度自動再平衡
3. 支援「月 K 級別真實股數累積回測法 (Share-Based Monthly Accounting)」：
   - 每月初以當日收盤價換算買入股數
   - 除息日精確計算持有股數分紅與 DRIP 零股買入
   - 每月底最後交易日收盤價結算總市值
   - 期初第 0 點 (T0) 精確錨定單筆本金 (例如 30 萬)
4. 個人月薪/年薪成長推算退休資產，整合「勞保老年給付 (A/B式擇優月領或一次領)」與「勞退新制個人專戶 (雇主6%+自提0~6%)」，
   並同時模擬 5 種退休提領規則（4%法則、Guyton-Klinger動態護欄、固定比例、領息優先、國人薪資中位數提領法）。
5. 支援貸款投入股市與自償模擬（外部償還、純賣股自償、80/20 現金保留池 + 定期借新還舊動態再平衡）。
"""

from dataclasses import dataclass
from typing import List, Dict, Optional
import math


# Task 2.1: 行政院主計總處各行業薪資中位數基準表 (全年總薪資中位數 ÷ 12)
GOV_SALARY_MEDIANS = [
    {"code": "CUSTOM", "name": "自訂輸入月薪", "monthly_median": None},
    {"code": "ALL_TW", "name": "🇹🇼 全體受僱員工中位數 (約 4.4 萬/月)", "monthly_median": 44000, "annual_median": 528000},
    {"code": "FINANCE", "name": "🏦 金融及保險業中位數 (約 8.9 萬/月)", "monthly_median": 89000, "annual_median": 1068000},
    {"code": "IT_COMM", "name": "💻 資訊及通訊傳播業中位數 (約 6.6 萬/月)", "monthly_median": 66000, "annual_median": 792000},
    {"code": "PRO_TECH", "name": "🔬 專業、科學及技術服務業 (約 5.5 萬/月)", "monthly_median": 55000, "annual_median": 660000},
    {"code": "HEALTH", "name": "🏥 醫療保健及社會工作服務業 (約 5.4 萬/月)", "monthly_median": 54000, "annual_median": 648000},
    {"code": "MFG", "name": "🏭 製造業中位數 (約 4.6 萬/月)", "monthly_median": 46000, "annual_median": 552000},
    {"code": "TRANS", "name": "🚢 運輸及倉儲業中位數 (約 5.1 萬/月)", "monthly_median": 51000, "annual_median": 612000},
    {"code": "CONST", "name": "🏗️ 營建工程業中位數 (約 4.3 萬/月)", "monthly_median": 43000, "annual_median": 516000},
    {"code": "RETAIL", "name": "🛍️ 批發及零售業中位數 (約 4.1 萬/月)", "monthly_median": 41000, "annual_median": 492000},
    {"code": "EDU", "name": "🎓 教育業（不含公立學校教師）(約 3.3 萬/月)", "monthly_median": 33000, "annual_median": 396000},
    {"code": "HOSP_FOOD", "name": "🍽️ 住宿及餐飲業中位數 (約 3.3 萬/月)", "monthly_median": 33000, "annual_median": 396000},
]


@dataclass
class RetirementInput:
    current_age: int = 32
    retire_age: int = 60
    max_age: int = 90
    monthly_salary: float = 65000.0          # 目前月薪 (TWD)
    initial_capital: float = 500000.0        # 目前已備退休本金 (TWD)
    saving_rate: float = 0.25                # 每月投資佔月薪比例 (例：25%)
    salary_growth_rate: float = 0.03         # 每年薪資成長率 (例：3%)
    salary_linked_dca: bool = True           # 是否隨加薪同步提高每月定期定額
    salary_mode: str = "fixed_growth"        # "fixed_growth", "stepped_5yr", "stepped_10yr", "custom_yearly"
    custom_salary_map: Optional[Dict[int, float]] = None  # Task 2.2: 各年齡對應月薪 (若提供則優先取用)
    expected_annual_return: float = 0.108    # 投資組合預期年化總報酬 (例：10.8%)
    expected_dividend_yield: float = 0.032   # 其中平均稅後現金殖利率 (例：3.2%)
    retire_monthly_spend_today: float = 50000.0  # 期望退休月生活費 (以今日物價計)
    inflation_rate: float = 0.022            # 預估年通膨率 (例：2.2%)
    withdraw_timing: str = "annual_boy"      # Task 2.4: "annual_boy" (每年初一次提領), "monthly" (每月定期提領)
    withdraw_rule: str = "rule_4pct"         # 主要檢視之提領規則 (包含 "rule_median_salary")
    median_salary_ratio: float = 1.0         # Task 2.5: 國人薪資中位數提領比例 (例如 1.0 = 100%, 0.8 = 80%)
    extra_expenses: Optional[List[Dict]] = None  # Task 2.3: 額外大筆花費清單
    # Stage 3 新增：勞保老年給付與勞退新制參數
    labor_ins_enabled: bool = True           # 是否計入勞保老年給付
    labor_ins_mode: str = "annuity"          # "annuity" (月領老年年金 A/B擇優), "lump_sum" (一次請領)
    labor_ins_start_work_age: int = 24       # 勞保起保年齡 (用於隨退休年齡連動累計年資)
    labor_ins_claim_age: int = 65            # 勞保預計請領年齡 (60~70歲，法定65歲，每提前/延後1年 ±4%)
    labor_ins_avg_wage: float = 45800.0      # 最高60個月平均投保薪資 (上限 45,800 元)
    labor_pen_enabled: bool = True           # 是否計入勞工退休金新制 (個人專戶)
    labor_pen_mode: str = "monthly"          # "monthly" (月退休金攤提), "lump_sum" (一次領取併入資產池)
    labor_pen_emp_rate: float = 0.06         # 雇主強制提撥率 (預設 6%)
    labor_pen_self_rate: float = 0.0         # 個人自願提繳率 (0% ~ 6%)
    labor_pen_init_balance: float = 300000.0 # 目前勞退個人專戶已累積本金與收益
    labor_pen_return_rate: float = 0.04      # 勞退新制基金預估年化收益率 (預設 4.0%)
    # Stage 3 新增：頁籤 3 貸款策略帶入之期滿淨資產注入
    loan_inject_enabled: bool = False        # 是否帶入頁籤 3 貸款策略成果
    loan_inject_age: int = 52                # 貸款策略期滿注入之年齡
    loan_inject_amount: float = 0.0          # 貸款策略期滿注入之淨資產金額


@dataclass
class LoanSimInput:
    loan_amount: float = 1500000.0           # 貸款總金額 (預設 150 萬)
    loan_term_months: int = 84               # 單筆貸款期數 (預設 7 年 84 期)
    annual_interest_rate: float = 0.05       # 貸款年利率 (預設 5.0%)
    grace_period_months: int = 0             # 寬限期月數 (只繳息不還本)
    repay_schedule: str = "amortized"        # "amortized" (本息均攤), "equal_principal" (本金均攤), "interest_only" (按月繳息到期還本)
    repay_source: str = "self_buffer_refi"   # "external" (外部薪資償還), "self_pure" (100%買股純賣股自償), "self_buffer_refi" (現金池+動態再平衡自償)
    equity_allocation_pct: float = 0.80      # 期初投入股市比例 (如 80% 買 ETF, 20% 放現金保留池)
    cash_pool_annual_yield: float = 0.0      # 現金保留池活存年利率
    refi_enabled: bool = True                # 是否啟用定期借新還舊重平衡
    refi_interval_months: int = 48           # 每滿幾期借新還舊並重平衡 (預設 48 期 = 4 年)
    total_sim_months: int = 240              # 總模擬期數 (預設 240 個月 = 20 年)
    expected_annual_return: float = 0.14     # 固定報酬推演模式下之預期年化總報酬 (預設 14.0%)
    post_payoff_dca: bool = True             # 若為外部償還且模擬期大於貸款期，期滿後原月付金是否繼續定期定額買股


@dataclass
class MonthlyBacktestInput:
    lump_sum: float = 300000.0
    monthly_dca: float = 15000.0
    dca_growth_rate: float = 0.0             # 每年隨薪資成長調升定期定額之比例
    drip: bool = True                        # 股息是否再投入
    apply_us_tax: bool = True                # 美股是否扣除 30% 預扣稅
    rebalance_yearly: bool = True            # 多標的是否每年底自動再平衡


def simulate_monthly_share_based(
    monthly_data_by_ticker: Dict[str, List[Dict]],
    weights_by_ticker: Dict[str, float],
    params: MonthlyBacktestInput,
) -> Dict:
    """
    執行月 K 級別真實股數累積回測 (Share-Based Monthly Accounting)
    """
    tickers = list(weights_by_ticker.keys())
    if not tickers:
        return {}

    months_set = None
    for t in tickers:
        t_months = [m["year_month"] for m in monthly_data_by_ticker.get(t, [])]
        if months_set is None:
            months_set = set(t_months)
        else:
            months_set = months_set.intersection(set(t_months))

    common_months = sorted(list(months_set)) if months_set else []
    if not common_months:
        return {}

    lookup = {t: {m["year_month"]: m for m in monthly_data_by_ticker[t]} for t in tickers}

    total_months = len(common_months)
    shares_held = {t: 0.0 for t in tickers}
    cash_bucket = 0.0
    cum_principal = params.lump_sum

    first_month_str = common_months[0]
    first_year = int(first_month_str.split("-")[0])

    if params.lump_sum > 0:
        for t in tickers:
            w = weights_by_ticker[t]
            p_start = lookup[t][first_month_str]["start_price"]
            if p_start > 0:
                shares_held[t] = (params.lump_sum * w) / p_start

    timeline_labels = [f"{first_month_str} 初 (起點)"]
    wealth_series = [params.lump_sum if params.lump_sum > 0 else params.monthly_dca]
    principal_series = [cum_principal]
    dividends_series = [0.0]

    total_dividends_collected = 0.0
    peak_wealth = wealth_series[0] if wealth_series[0] > 0 else 1.0
    max_drawdown = 0.0

    for m_idx, m_str in enumerate(common_months):
        yr = int(m_str.split("-")[0])
        yr_offset = yr - first_year

        cur_monthly_dca = params.monthly_dca * ((1.0 + params.dca_growth_rate) ** yr_offset)

        if cur_monthly_dca > 0:
            cum_principal += cur_monthly_dca
            for t in tickers:
                w = weights_by_ticker[t]
                p_start = lookup[t][m_str]["start_price"]
                if p_start > 0:
                    shares_held[t] += (cur_monthly_dca * w) / p_start

        month_div_total = 0.0
        for t in tickers:
            m_data = lookup[t][m_str]
            div_per_share = m_data.get("div_cash", 0.0)
            tax_rate = m_data.get("tax_rate", 0.0) if params.apply_us_tax else 0.0
            if div_per_share > 0 and shares_held[t] > 0:
                net_div = shares_held[t] * div_per_share * (1.0 - tax_rate)
                month_div_total += net_div
                if params.drip:
                    p_end = m_data["end_price"]
                    if p_end > 0:
                        shares_held[t] += net_div / p_end
                else:
                    cash_bucket += net_div

        total_dividends_collected += month_div_total

        month_end_equity = cash_bucket
        for t in tickers:
            p_end = lookup[t][m_str]["end_price"]
            month_end_equity += shares_held[t] * p_end

        if params.rebalance_yearly and len(tickers) > 1:
            month_num = int(m_str.split("-")[1])
            if month_num == 12 or m_idx == total_months - 1:
                for t in tickers:
                    w = weights_by_ticker[t]
                    p_end = lookup[t][m_str]["end_price"]
                    if p_end > 0:
                        target_val = (month_end_equity - cash_bucket) * w
                        shares_held[t] = target_val / p_end

        timeline_labels.append(m_str)
        wealth_series.append(round(month_end_equity, 2))
        principal_series.append(round(cum_principal, 2))
        dividends_series.append(round(total_dividends_collected, 2))

        if month_end_equity > peak_wealth:
            peak_wealth = month_end_equity
        if peak_wealth > 0:
            dd = (month_end_equity - peak_wealth) / peak_wealth
            if dd < max_drawdown:
                max_drawdown = dd

    final_wealth = wealth_series[-1]
    years_float = max(0.083, total_months / 12.0)

    if cum_principal > 0 and final_wealth > 0:
        cagr = (final_wealth / cum_principal) ** (1.0 / years_float) - 1.0 if params.monthly_dca == 0 else \
               math.pow(max(0.01, final_wealth / cum_principal), 1.0 / (years_float * 0.65)) - 1.0
    else:
        cagr = 0.0

    monthly_rets = []
    for i in range(2, len(wealth_series)):
        prev_w = wealth_series[i - 1]
        cur_w = wealth_series[i]
        if prev_w > 0:
            monthly_rets.append((cur_w - prev_w) / prev_w)

    if monthly_rets:
        mean_m = sum(monthly_rets) / len(monthly_rets)
        var_m = sum((r - mean_m) ** 2 for r in monthly_rets) / len(monthly_rets)
        annual_vol = math.sqrt(var_m) * math.sqrt(12.0)
    else:
        annual_vol = 0.15

    sharpe = (cagr - 0.02) / annual_vol if annual_vol > 0 else 0.0
    avg_yield = (total_dividends_collected / cum_principal) / years_float if cum_principal > 0 else 0.0

    return {
        "start_month": common_months[0],
        "end_month": common_months[-1],
        "total_months": total_months,
        "years": round(years_float, 1),
        "final_wealth": round(final_wealth, 0),
        "total_principal": round(cum_principal, 0),
        "total_dividends": round(total_dividends_collected, 0),
        "cagr": round(cagr, 4),
        "volatility": round(annual_vol, 4),
        "max_drawdown": round(max_drawdown, 4),
        "sharpe": round(sharpe, 2),
        "avg_yield": round(avg_yield, 4),
        "timeline_labels": timeline_labels,
        "wealth_series": wealth_series,
        "principal_series": principal_series,
    }


def calculate_taiwan_statutory_pensions(params: RetirementInput, salary_by_age: Dict[int, float]) -> Dict:
    """
    Task 3.1: 精算台灣「勞保老年給付」與「勞工退休金新制（個人專戶）」
    """
    ins_years = max(0, params.retire_age - params.labor_ins_start_work_age)
    ins_wage = min(45800.0, max(11100.0, params.labor_ins_avg_wage))
    claim_ins_age = max(60, min(70, max(params.retire_age, params.labor_ins_claim_age)))
    diff_years = max(-5, min(5, claim_ins_age - 65))
    adj_factor = 1.0 + 0.04 * diff_years

    formula_a = (ins_wage * ins_years * 0.00775 + 3000.0) * adj_factor
    formula_b = (ins_wage * ins_years * 0.0155) * adj_factor
    best_monthly_ins = max(formula_a, formula_b) if (params.labor_ins_enabled and params.labor_ins_mode == "annuity") else 0.0

    if ins_years <= 15:
        ins_lump_months = ins_years
    else:
        ins_lump_months = 15 + (ins_years - 15) * 2
    ins_lump_months = min(50 if params.retire_age > 60 else 45, ins_lump_months)
    ins_lump_sum = (ins_wage * ins_lump_months) if (params.labor_ins_enabled and params.labor_ins_mode == "lump_sum") else 0.0

    pen_balance = params.labor_pen_init_balance if params.labor_pen_enabled else 0.0
    total_rate = params.labor_pen_emp_rate + params.labor_pen_self_rate
    claim_pen_age = max(60, params.retire_age)

    if params.labor_pen_enabled:
        for age in range(params.current_age, claim_pen_age):
            if age < params.retire_age:
                sal = salary_by_age.get(age, params.monthly_salary)
                capped_sal = min(150000.0, sal)
                annual_contrib = capped_sal * total_rate * 12.0
            else:
                annual_contrib = 0.0
            pen_balance = (pen_balance + annual_contrib * 0.5) * (1.0 + params.labor_pen_return_rate) + annual_contrib * 0.5

    life_expectancy_years = max(10, 84 - claim_pen_age)
    n_months = life_expectancy_years * 12
    r_m = params.labor_pen_return_rate / 12.0
    if params.labor_pen_enabled and params.labor_pen_mode == "monthly" and pen_balance > 0:
        if r_m > 0:
            monthly_pen = pen_balance * r_m / (1.0 - math.pow(1.0 + r_m, -n_months))
        else:
            monthly_pen = pen_balance / n_months
        pen_lump_sum = 0.0
    elif params.labor_pen_enabled and params.labor_pen_mode == "lump_sum":
        monthly_pen = 0.0
        pen_lump_sum = pen_balance
    else:
        monthly_pen = 0.0
        pen_lump_sum = 0.0

    return {
        "labor_ins_years": ins_years,
        "labor_ins_claim_age": claim_ins_age,
        "labor_ins_adj_factor": round(adj_factor, 2),
        "labor_ins_formula_a": round(formula_a, 0),
        "labor_ins_formula_b": round(formula_b, 0),
        "labor_ins_monthly": round(best_monthly_ins, 0),
        "labor_ins_lump_sum": round(ins_lump_sum, 0),
        "labor_pen_claim_age": claim_pen_age,
        "labor_pen_end_age": claim_pen_age + life_expectancy_years,
        "labor_pen_accumulated": round(pen_balance, 0),
        "labor_pen_monthly": round(monthly_pen, 0),
        "labor_pen_lump_sum": round(pen_lump_sum, 0),
        "combined_statutory_monthly": round(best_monthly_ins + monthly_pen, 0),
        "combined_statutory_lump_sum": round(ins_lump_sum + pen_lump_sum, 0),
    }


def simulate_lifecycle_and_withdrawals(
    params: RetirementInput,
    custom_market_returns: Optional[List[float]] = None,
) -> Dict:
    """
    執行從「目前年齡（資產累積期）」到「預期壽命（退休提領期）」之全生命週期現金流模擬，
    整合勞保老年給付、勞退新制專戶、以及頁籤 3 貸款期滿淨值注入。
    """
    years_to_retire = max(1, params.retire_age - params.current_age)

    cum_principal = params.initial_capital
    wealth_acc = params.initial_capital
    records = []

    def get_extra_expense_nominal(target_age: int, total_years_offset: int) -> float:
        if not params.extra_expenses:
            return 0.0
        total_e = 0.0
        for item in params.extra_expenses:
            if not item.get("enabled", True):
                continue
            s_age = item.get("start_age", params.current_age)
            e_age = item.get("end_age", params.max_age)
            interval = max(1, item.get("interval_years", 1))
            if s_age <= target_age <= e_age:
                if (target_age - s_age) % interval == 0:
                    amt_today = float(item.get("amount", 0.0))
                    total_e += amt_today * ((1.0 + params.inflation_rate) ** total_years_offset)
        return total_e

    salary_by_age = {}
    for yr_idx in range(years_to_retire):
        age = params.current_age + yr_idx
        if params.custom_salary_map and age in params.custom_salary_map:
            salary_by_age[age] = params.custom_salary_map[age]
        else:
            salary_by_age[age] = params.monthly_salary * ((1.0 + params.salary_growth_rate) ** yr_idx)

    pension_info = calculate_taiwan_statutory_pensions(params, salary_by_age)

    # 1. 工作累積期 (current_age -> retire_age - 1)
    for yr_idx in range(years_to_retire):
        age = params.current_age + yr_idx
        cur_monthly_sal = salary_by_age[age]

        monthly_invest = (
            cur_monthly_sal * params.saving_rate
            if params.salary_linked_dca
            else params.monthly_salary * params.saving_rate
        )
        annual_invest = monthly_invest * 12.0

        loan_inflow = params.loan_inject_amount if (params.loan_inject_enabled and age == params.loan_inject_age) else 0.0

        mkt_ret = (
            custom_market_returns[yr_idx % len(custom_market_returns)]
            if custom_market_returns
            else params.expected_annual_return
        )
        price_ret = mkt_ret - params.expected_dividend_yield

        avg_cap = wealth_acc + annual_invest * 0.5 + loan_inflow
        div_cash = max(0.0, avg_cap * params.expected_dividend_yield)
        price_gain = avg_cap * price_ret

        extra_e = get_extra_expense_nominal(age, yr_idx)

        wealth_acc = max(0.0, wealth_acc + annual_invest + loan_inflow + div_cash + price_gain - extra_e)
        cum_principal += annual_invest

        records.append({
            "age": age,
            "phase": "累積期",
            "market_return": round(mkt_ret, 4),
            "monthly_salary": round(cur_monthly_sal, 0),
            "annual_invest": round(annual_invest, 0),
            "loan_inflow": round(loan_inflow, 0),
            "statutory_pension_annual": 0,
            "dividend_earned": round(div_cash, 0),
            "extra_expense": round(extra_e, 0),
            "withdraw_4pct": 0,
            "withdraw_gk": 0,
            "withdraw_fixed_pct": 0,
            "withdraw_div_first": 0,
            "withdraw_median_salary": 0,
            "wealth_4pct": round(wealth_acc, 0),
            "wealth_gk": round(wealth_acc, 0),
            "wealth_fixed_pct": round(wealth_acc, 0),
            "wealth_div_first": round(wealth_acc, 0),
            "wealth_median_salary": round(wealth_acc, 0),
        })

    if params.labor_ins_enabled and params.labor_ins_mode == "lump_sum":
        wealth_acc += pension_info["labor_ins_lump_sum"]
    if params.labor_pen_enabled and params.labor_pen_mode == "lump_sum" and params.retire_age >= 60:
        wealth_acc += pension_info["labor_pen_lump_sum"]

    wealth_at_retirement = wealth_acc
    inf_at_retire = (1.0 + params.inflation_rate) ** years_to_retire
    first_yr_target_spend_nominal = (params.retire_monthly_spend_today * 12.0) * inf_at_retire

    steady_statutory_annual = (pension_info["labor_ins_monthly"] + pension_info["labor_pen_monthly"]) * 12.0
    gap_years_ins = max(0, pension_info["labor_ins_claim_age"] - params.retire_age)
    bridge_needed = gap_years_ins * (pension_info["labor_ins_monthly"] * 12.0)
    net_self_annual_spend = max(0.0, first_yr_target_spend_nominal - steady_statutory_annual)
    target_wealth_4pct = (net_self_annual_spend / 0.04) + bridge_needed

    # 2. 退休提領期 (retire_age -> max_age)
    w_4pct = wealth_at_retirement
    w_gk = wealth_at_retirement
    w_fixed = wealth_at_retirement
    w_div = wealth_at_retirement
    w_median = wealth_at_retirement

    cur_spend_4pct = first_yr_target_spend_nominal
    cur_spend_gk = first_yr_target_spend_nominal
    initial_gk_rate = (net_self_annual_spend / wealth_at_retirement) if wealth_at_retirement > 0 else 0.04
    national_median_monthly_base = 44000.0

    def apply_withdrawal_timing(w_start: float, net_portfolio_withdraw: float, r_tot: float) -> tuple:
        actual_w = min(w_start, max(0.0, net_portfolio_withdraw))
        if params.withdraw_timing == "monthly":
            w_next = max(0.0, w_start * (1.0 + r_tot) - actual_w * (1.0 + 0.5 * r_tot))
        else:
            rem = max(0.0, w_start - actual_w)
            w_next = max(0.0, rem * (1.0 + r_tot))
        return w_next, actual_w

    for ret_idx in range(params.max_age - params.retire_age + 1):
        age = params.retire_age + ret_idx
        total_idx = years_to_retire + ret_idx
        inf_factor = (1.0 + params.inflation_rate) ** total_idx

        pen_lump_inflow = (
            pension_info["labor_pen_lump_sum"]
            if (params.labor_pen_enabled and params.labor_pen_mode == "lump_sum" and params.retire_age < 60 and age == 60)
            else 0.0
        )
        loan_inflow = params.loan_inject_amount if (params.loan_inject_enabled and age == params.loan_inject_age) else 0.0
        inflow_sum = pen_lump_inflow + loan_inflow
        if inflow_sum > 0:
            w_4pct += inflow_sum
            w_gk += inflow_sum
            w_fixed += inflow_sum
            w_div += inflow_sum
            w_median += inflow_sum

        cur_ins_annual = (pension_info["labor_ins_monthly"] * 12.0) if age >= pension_info["labor_ins_claim_age"] else 0.0
        cur_pen_annual = (
            (pension_info["labor_pen_monthly"] * 12.0)
            if (age >= pension_info["labor_pen_claim_age"] and age <= pension_info["labor_pen_end_age"])
            else 0.0
        )
        statutory_annual = cur_ins_annual + cur_pen_annual

        mkt_ret = (
            custom_market_returns[total_idx % len(custom_market_returns)]
            if custom_market_returns
            else params.expected_annual_return
        )
        price_ret = mkt_ret - params.expected_dividend_yield
        total_ret = mkt_ret

        extra_e = get_extra_expense_nominal(age, total_idx)

        # (A) 經典 4% 法則
        if ret_idx > 0:
            cur_spend_4pct *= (1.0 + params.inflation_rate)
        target_4_net = max(0.0, cur_spend_4pct + extra_e - statutory_annual)
        w_4pct, withdraw_4 = apply_withdrawal_timing(w_4pct, target_4_net, total_ret)

        # (B) Guyton-Klinger 動態護欄提領
        if ret_idx > 0 and w_gk > 0:
            if mkt_ret >= 0:
                cur_spend_gk *= (1.0 + params.inflation_rate)
            cur_rate = max(0.0, cur_spend_gk - statutory_annual) / w_gk
            if cur_rate > initial_gk_rate * 1.20:
                cur_spend_gk *= 0.90
            elif cur_rate < initial_gk_rate * 0.80:
                cur_spend_gk *= 1.10
        target_gk_net = max(0.0, cur_spend_gk + extra_e - statutory_annual)
        w_gk, withdraw_gk = apply_withdrawal_timing(w_gk, target_gk_net, total_ret)

        # (C) 固定比例提領法
        target_fixed = (w_fixed * 0.045) + extra_e
        w_fixed, withdraw_fixed = apply_withdrawal_timing(w_fixed, target_fixed, total_ret)

        # (D) 存股領息優先 + 差額賣股法
        target_spend_div_net = max(0.0, (params.retire_monthly_spend_today * 12.0 * inf_factor) + extra_e - statutory_annual)
        gross_div = w_div * params.expected_dividend_yield
        if gross_div >= target_spend_div_net:
            withdraw_div = target_spend_div_net
            surplus = gross_div - target_spend_div_net
            w_div = max(0.0, w_div * (1.0 + price_ret) + surplus)
        else:
            need_sell = target_spend_div_net - gross_div
            withdraw_div = min(w_div + gross_div, target_spend_div_net)
            base_after_sell = max(0.0, w_div - need_sell)
            if params.withdraw_timing == "monthly":
                w_div = max(0.0, base_after_sell * (1.0 + price_ret) + need_sell * 0.5 * price_ret)
            else:
                w_div = max(0.0, base_after_sell * (1.0 + price_ret))

        # (E) 國人薪資中位數提領法
        target_median_net = max(0.0, (national_median_monthly_base * 12.0 * params.median_salary_ratio * inf_factor) + extra_e - statutory_annual)
        w_median, withdraw_median = apply_withdrawal_timing(w_median, target_median_net, total_ret)

        records.append({
            "age": age,
            "phase": "退休期",
            "market_return": round(mkt_ret, 4),
            "monthly_salary": 0,
            "annual_invest": 0,
            "loan_inflow": round(inflow_sum, 0),
            "statutory_pension_annual": round(statutory_annual, 0),
            "dividend_earned": round(gross_div, 0),
            "extra_expense": round(extra_e, 0),
            "withdraw_4pct": round(withdraw_4, 0),
            "withdraw_gk": round(withdraw_gk, 0),
            "withdraw_fixed_pct": round(withdraw_fixed, 0),
            "withdraw_div_first": round(withdraw_div, 0),
            "withdraw_median_salary": round(withdraw_median, 0),
            "wealth_4pct": round(w_4pct, 0),
            "wealth_gk": round(w_gk, 0),
            "wealth_fixed_pct": round(w_fixed, 0),
            "wealth_div_first": round(w_div, 0),
            "wealth_median_salary": round(w_median, 0),
        })

    return {
        "summary": {
            "total_principal_invested": round(cum_principal, 0),
            "wealth_at_retirement_nominal": round(wealth_at_retirement, 0),
            "wealth_at_retirement_real": round(wealth_at_retirement / inf_at_retire, 0),
            "target_wealth_4pct_nominal": round(target_wealth_4pct, 0),
            "achievement_rate_pct": round((wealth_at_retirement / target_wealth_4pct) * 100.0, 1)
            if target_wealth_4pct > 0 else 100.0,
            "pension_info": pension_info,
        },
        "yearly_records": records,
    }


def simulate_loan_investment(
    params: LoanSimInput,
    historical_monthly_returns: Optional[List[Dict]] = None,
) -> Dict:
    """
    Task 3.3: 貸款投入股市與自償模擬引擎
    支援：
    1. 外部薪資另外償還 (external)
    2. 100% 買股純賣股自償 (self_pure)
    3. 現金保留池 + 定期借新還舊動態再平衡自償 (self_buffer_refi，如 150 萬信貸 80/20 正 2 ETF 48 期循環)
    可搭配固定預期年化報酬率或 SQLite 真實歷史月 K 數據。
    """
    L0 = params.loan_amount
    N = max(1, params.loan_term_months)
    r_loan_m = params.annual_interest_rate / 12.0
    grace_m = min(N - 1, max(0, params.grace_period_months))

    def get_loan_schedule_step(loan_principal: float, cycle_month_idx: int) -> tuple:
        if loan_principal <= 0 or cycle_month_idx > N:
            return 0.0, 0.0, 0.0, 0.0

        if params.repay_schedule == "interest_only":
            interest = loan_principal * r_loan_m
            return interest, interest, 0.0, loan_principal

        if cycle_month_idx <= grace_m:
            interest = loan_principal * r_loan_m
            return interest, interest, 0.0, loan_principal

        amort_months = N - grace_m
        k = cycle_month_idx - grace_m

        if params.repay_schedule == "equal_principal":
            prin_paid = loan_principal / amort_months
            prev_rem = loan_principal - prin_paid * (k - 1)
            interest = prev_rem * r_loan_m
            pmt = prin_paid + interest
            rem_bal = max(0.0, prev_rem - prin_paid)
            return pmt, interest, prin_paid, rem_bal
        else:
            if r_loan_m > 0:
                pmt = loan_principal * r_loan_m * math.pow(1.0 + r_loan_m, amort_months) / (math.pow(1.0 + r_loan_m, amort_months) - 1.0)
                rem_bal = loan_principal * (math.pow(1.0 + r_loan_m, amort_months) - math.pow(1.0 + r_loan_m, k)) / (math.pow(1.0 + r_loan_m, amort_months) - 1.0)
                prev_bal = loan_principal * (math.pow(1.0 + r_loan_m, amort_months) - math.pow(1.0 + r_loan_m, k - 1)) / (math.pow(1.0 + r_loan_m, amort_months) - 1.0)
            else:
                pmt = loan_principal / amort_months
                rem_bal = max(0.0, loan_principal - pmt * k)
                prev_bal = max(0.0, loan_principal - pmt * (k - 1))
            interest = prev_bal * r_loan_m
            prin_paid = prev_bal - rem_bal
            return pmt, interest, prin_paid, max(0.0, rem_bal)

    total_months = max(1, params.total_sim_months)
    if historical_monthly_returns:
        total_months = min(total_months, len(historical_monthly_returns))

    fixed_r_m = math.pow(1.0 + params.expected_annual_return, 1.0 / 12.0) - 1.0
    cash_r_m = params.cash_pool_annual_yield / 12.0

    eq_ratio = 1.0 if params.repay_source in ("external", "self_pure") else max(0.1, min(1.0, params.equity_allocation_pct))
    etf_val = L0 * eq_ratio
    cash_pool = L0 * (1.0 - eq_ratio)
    active_loan_principal = L0
    cycle_month = 0
    cycle_count = 1
    cycle_start_pool = L0
    cycle_init_etf = etf_val
    cycle_init_cash = cash_pool
    cycle_forced_sell = 0.0

    total_forced_sell = 0.0
    total_external_paid = 0.0
    depleted_month_label = None
    unleveraged_wealth = 0.0

    timeline_labels = ["T0 期初"]
    total_asset_series = [round(etf_val + cash_pool, 0)]
    net_equity_series = [round(etf_val + cash_pool - active_loan_principal, 0)]
    debt_series = [round(active_loan_principal, 0)]
    baseline_series = [0.0]

    cycle_milestones = []
    monthly_records = []

    first_pmt, _, _, _ = get_loan_schedule_step(L0, min(N, grace_m + 1))

    for m_idx in range(1, total_months + 1):
        cycle_month += 1
        if historical_monthly_returns and (m_idx - 1) < len(historical_monthly_returns):
            m_info = historical_monthly_returns[m_idx - 1]
            r_eq = m_info["total_return"]
            ym_label = m_info["year_month"]
        else:
            r_eq = fixed_r_m
            yr_num = (m_idx - 1) // 12 + 1
            mo_num = (m_idx - 1) % 12 + 1
            ym_label = f"第{yr_num}年M{mo_num:02d}"

        pmt, interest, prin_paid, rem_debt = get_loan_schedule_step(active_loan_principal, cycle_month)

        forced_sell_this_month = 0.0
        ext_paid_this_month = 0.0

        if params.repay_source == "external":
            if pmt > 0:
                ext_paid_this_month = pmt
                total_external_paid += pmt
                unleveraged_wealth = (unleveraged_wealth + pmt) * (1.0 + r_eq)
            elif params.post_payoff_dca and first_pmt > 0:
                ext_paid_this_month = first_pmt
                total_external_paid += first_pmt
                etf_val += first_pmt
                unleveraged_wealth = (unleveraged_wealth + first_pmt) * (1.0 + r_eq)
            else:
                unleveraged_wealth = unleveraged_wealth * (1.0 + r_eq)
            etf_val = etf_val * (1.0 + r_eq)
        else:
            cash_pool = cash_pool * (1.0 + cash_r_m)
            if pmt > 0:
                if cash_pool >= pmt:
                    cash_pool -= pmt
                    etf_val = etf_val * (1.0 + r_eq)
                else:
                    shortfall = pmt - cash_pool
                    cash_pool = 0.0
                    etf_after_growth = etf_val * (1.0 + r_eq)
                    if etf_after_growth >= shortfall:
                        etf_val = etf_after_growth - shortfall
                        forced_sell_this_month = shortfall
                    else:
                        forced_sell_this_month = etf_after_growth
                        etf_val = 0.0
                        if depleted_month_label is None:
                            depleted_month_label = ym_label
            else:
                etf_val = etf_val * (1.0 + r_eq)

            cycle_forced_sell += forced_sell_this_month
            total_forced_sell += forced_sell_this_month

        gross_asset = etf_val + cash_pool
        net_equity = gross_asset - rem_debt

        refi_triggered = False
        refi_cash_out = 0.0
        if (
            params.repay_source == "self_buffer_refi"
            and params.refi_enabled
            and params.refi_interval_months > 0
            and cycle_month == params.refi_interval_months
        ):
            cycle_milestones.append({
                "cycle": cycle_count,
                "end_month_index": m_idx,
                "label": ym_label,
                "start_pool": round(cycle_start_pool, 0),
                "init_etf": round(cycle_init_etf, 0),
                "init_cash": round(cycle_init_cash, 0),
                "forced_sell": round(cycle_forced_sell, 0),
                "end_etf": round(etf_val, 0),
                "old_debt_payoff": round(rem_debt, 0),
                "net_equity": round(net_equity, 0),
            })
            if m_idx < total_months:
                refi_triggered = True
                refi_cash_out = max(0.0, L0 - rem_debt)
                new_total_pool = etf_val + cash_pool + refi_cash_out
                etf_val = new_total_pool * eq_ratio
                cash_pool = new_total_pool * (1.0 - eq_ratio)
                active_loan_principal = L0
                rem_debt = L0
                gross_asset = etf_val + cash_pool
                net_equity = gross_asset - rem_debt

                cycle_count += 1
                cycle_month = 0
                cycle_start_pool = new_total_pool
                cycle_init_etf = etf_val
                cycle_init_cash = cash_pool
                cycle_forced_sell = 0.0

        timeline_labels.append(ym_label)
        total_asset_series.append(round(gross_asset, 0))
        net_equity_series.append(round(net_equity, 0))
        debt_series.append(round(rem_debt, 0))
        baseline_series.append(round(unleveraged_wealth, 0))

        monthly_records.append({
            "month_index": m_idx,
            "label": ym_label,
            "pmt": round(pmt, 0),
            "interest": round(interest, 0),
            "principal_paid": round(prin_paid, 0),
            "forced_sell": round(forced_sell_this_month, 0),
            "external_paid": round(ext_paid_this_month, 0),
            "cash_pool": round(cash_pool, 0),
            "etf_value": round(etf_val, 0),
            "remaining_debt": round(rem_debt, 0),
            "net_equity": round(net_equity, 0),
            "refi_triggered": refi_triggered,
            "refi_cash_out": round(refi_cash_out, 0),
        })

    if not cycle_milestones or cycle_milestones[-1]["end_month_index"] != total_months:
        cycle_milestones.append({
            "cycle": cycle_count,
            "end_month_index": total_months,
            "label": timeline_labels[-1],
            "start_pool": round(cycle_start_pool, 0),
            "init_etf": round(cycle_init_etf, 0),
            "init_cash": round(cycle_init_cash, 0),
            "forced_sell": round(cycle_forced_sell, 0),
            "end_etf": round(etf_val, 0),
            "old_debt_payoff": round(debt_series[-1], 0),
            "net_equity": round(net_equity_series[-1], 0),
        })

    return {
        "summary": {
            "loan_amount": round(L0, 0),
            "monthly_pmt": round(first_pmt, 0),
            "total_months": total_months,
            "final_etf_value": round(etf_val, 0),
            "final_cash_pool": round(cash_pool, 0),
            "final_gross_asset": round(total_asset_series[-1], 0),
            "final_remaining_debt": round(debt_series[-1], 0),
            "final_net_equity": round(net_equity_series[-1], 0),
            "total_forced_sell": round(total_forced_sell, 0),
            "total_external_paid": round(total_external_paid, 0),
            "unleveraged_final_wealth": round(baseline_series[-1], 0),
            "depleted_month": depleted_month_label,
        },
        "cycle_milestones": cycle_milestones,
        "timeline_labels": timeline_labels,
        "total_asset_series": total_asset_series,
        "net_equity_series": net_equity_series,
        "debt_series": debt_series,
        "baseline_series": baseline_series,
        "monthly_records": monthly_records,
    }


if __name__ == "__main__":
    demo_input = RetirementInput()
    result = simulate_lifecycle_and_withdrawals(demo_input)
    print("=== RetirePilot 退休推算摘要 ===")
    for k, v in result["summary"].items():
        if isinstance(v, (int, float)):
            print(f"  {k}: {v:,.0f}" if v > 1000 else f"  {k}: {v}%")
    loan_demo = LoanSimInput()
    loan_res = simulate_loan_investment(loan_demo)
    print("\n=== 信貸 150 萬正2 80/20 借新還舊 5 次循環 (20年) 驗證 ===")
    for c in loan_res["cycle_milestones"]:
        print(f"  第 {c['cycle']} 次循環: 期初資金池={c['start_pool']:,.0f}, 被迫賣股={c['forced_sell']:,.0f}, 期末ETF={c['end_etf']:,.0f}, 期末淨資產={c['net_equity']:,.0f}")
