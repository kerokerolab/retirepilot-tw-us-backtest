# RetirePilot 跨市場退休理財模擬系統 — 工程開發執行規格書 (`TODO_FOR_FLASH.md`)

> **給執行模型（Flash / Builder）之強制施工守則**：
> 1. **嚴禁簡化財務數學公式**：所有回測與退休現金流計算必須 100% 遵循本規格書定義之公式與資料結構，不得自行退回粗略概算。
> 2. **本機檔案工具呼叫限制**：呼叫 `client:write_to_file` 或 `client:replace_file_content` 時，**每一輪（Turn）只能單獨呼叫 1 次 `client:*` 工具（序列執行）**，嚴禁在同一輪平行發出多個 `client:*` 呼叫，否則會觸發 `Session is already connected` 錯誤。
> 3. **完整保留既有架構**：修改 `backend_mvp/app.py`、`backend_mvp/backtest_and_retire_engine.py` 與 `prototype/retire_backtest_mvp.html` 時，必須完整保留已實作之 **400 檔標的池 (`universe_400.py`)** 與 **未滿 20 年標的三級分流機制（`strict_truncate` / `whitelist_proxy` / `dynamic_parking` ＋ 短樣本貝氏收斂保護）**。
> 4. **小步快跑驗收**：請依序按 **Stage 1（回測精準度與起點修正）➔ Stage 2（Phase 2 五大進階功能）** 逐步實作，每完成一個 Stage 即向使用者回報並確認。

---

## Stage 1：回測引擎精準度升級（折線圖起點 $T_0$ 修正 ＋ 投入模式切換 ＋ 月 K 真實股數累積法）

### Task 1.1：新增「投入資金模式」三選一切換（防呆機制）
- **修改目標**：`prototype/retire_backtest_mvp.html`（頁籤 2 左側「4. 投入資金與股息再投入設定」區塊）
- **規格細節**：
  1. 新增下拉選單或分段按鈕 `#bt-investMode`：
     - `both`（預設）：**單筆投入 ＋ 每月定期定額**（兩個輸入框皆可編輯）。
     - `lump_only`：**純單筆投入**（自動將 `#bt-monthlyDCA` 設為 `0` 並設為 `disabled` 灰底，避免測試單筆 30 萬時誤把定期定額算進去）。
     - `dca_only`：**純定期定額投入**（自動將 `#bt-lumpSum` 設為 `0` 並設為 `disabled` 灰底）。

### Task 1.2：修正折線圖第 0 點（$T_0$ 期初投入起點）與年中定期定額權重
- **修改目標**：`prototype/retire_backtest_mvp.html` 之 `simulatePathFromStartIndex()` 與 `YEARS` 座標軸
- **問題根因**：原本折線圖的第一個點直接畫「第一年年底（12/31）結算後資產」，導致單筆投入 30 萬選 `00631L.TW` 時，第一個點顯示 37.5 萬（純單筆）或 60.0 萬（含定期定額），看不見期初剛投入時的 **30.0 萬**。
- **修正公式**：
  1. 在回測起算點（`startIndex` 對應之年初，標記為 `YYYY年初(起點)` 或在序列最前端加入起點座標），**第一個顯示點必須精確錨定為期初投入本金**：
     $$W_{t_0} = \begin{cases} \text{lumpSum}, & \text{if } \text{lumpSum} > 0 \\ \text{monthlyDCA}, & \text{if } \text{lumpSum} = 0 \end{cases}$$
     因此當使用者設定單筆投入 30 萬、定期定額 0 元時，折線圖上的**第一個點必定精準顯示 `NT$ 30.0 萬`（300,000 元）**！
  2. **年度備援模式下之年中定期定額報酬權重校正**：
     當處於未連線 SQLite 之年度備援模式時，期初既有資產 `sharesValue` 享有整年漲跌幅 `(1 + pRet)`，而當年度分 12 個月投入之定期定額 `annualAdd` 平均在市場停留半年，應乘上 `(1 + 0.5 * pRet)`：
     $$\text{divCash}_y = \max\left(0,\; (\text{sharesValue}_{y-1} + 0.5 \times \text{annualAdd}_y) \times d_y\right)$$
     $$\text{sharesValue}_y = \text{sharesValue}_{y-1} \times (1 + r_{\text{price}, y}) + \text{annualAdd}_y \times (1 + 0.5 \times r_{\text{price}, y}) + (\text{divCash}_y \text{ if DRIP else } 0)$$

### Task 1.3：後端 SQLite 升級為「月 K 級別真實股數累積回測引擎 (Share-Based Monthly Accounting)」
- **修改目標**：`backend_mvp/app.py` 與 `backend_mvp/backtest_and_retire_engine.py`
- **核心演算法（當 SQLite `daily_prices` 已同步該標的日 K 資料時啟動）**：
  1. **提取月度價格與當月除息表**：
     - 從 `daily_prices` 讀取該標的自 `2006-01-01` 起之每日收盤價，按年月 `YYYY-MM` 分組，取得每個月的：
       - `first_open_close`：當月第一個交易日收盤價 $P_{m, \text{start}}$（作為每月定期定額扣款買入價）
       - `last_close`：當月最後一個交易日收盤價 $P_{m, \text{end}}$（作為月底結算市值價）
     - 從 `corporate_dividends` 讀取當月發生之每股現金股利 $d_{m, \text{cash}}$ 與股票股利 $d_{m, \text{stock}}$（美股若開啟預扣稅則乘上 $1 - 0.30$）。
  2. **逐月真實股數累積迴圈（共 240 個月）**：
     - **期初（$T_0$，第一個有效交易日）**：
       $$\text{Shares}_0 = \frac{\text{lumpSum}}{P_{1, \text{start}}}, \quad \text{Wealth}_0 = \text{lumpSum}$$
     - **第 $m$ 個月月初（定期定額扣款買入）**：
       $$\Delta \text{Shares}_{\text{dca}, m} = \frac{C_m}{P_{m, \text{start}}}, \quad \text{Shares}_m \leftarrow \text{Shares}_{m-1} + \Delta \text{Shares}_{\text{dca}, m}$$
     - **第 $m$ 個月除權息處理**：
       $$\text{DivCash}_m = \text{Shares}_m \times d_{m, \text{cash}} \times (1 - \tau_{\text{tax}})$$
       - 若為 **DRIP（股息再投入）**：按當月收盤價買入零股 $\text{Shares}_m \leftarrow \text{Shares}_m + \frac{\text{DivCash}_m}{P_{m, \text{end}}}$
       - 若為 **股息領現**：$\text{CashBucket}_m \leftarrow \text{CashBucket}_{m-1} + \text{DivCash}_m$
     - **第 $m$ 個月月底結算市值（Mark-to-Market）**：
       $$\text{Wealth}_m = \text{Shares}_m \times P_{m, \text{end}} + \text{CashBucket}_m$$
  3. **多標的組合年度再平衡與月度最大回撤 (Monthly Max Drawdown)**：
     - 每年 12 月底最後一個交易日，將組合內各標的市值按目標權重 $w_i$ 重新分配買賣股數。
     - 以 240 個月度結算市值序列計算真實的峰值對谷底最大回撤（Max Drawdown），完整捕捉如 2020 年 3 月疫情熔斷或 2022 年月度回檔。

---

## Stage 2：Phase 2 五大進階功能實作規格

### Task 2.1：內建行政院主計總處「全體與各行業全年總薪資中位數 ÷ 12」選單
- **修改目標**：`backend_mvp/universe_400.py`（或新增 `backend_mvp/gov_salary_data.py`）與 `prototype/retire_backtest_mvp.html` 頁籤 1
- **內建主計總處基準資料表（單位：新台幣元，口徑：全年總薪資中位數 ÷ 12，含經常性薪資＋年終獎金＋績效獎金攤提）**：
  ```python
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
  ```
- **UI 與互動規範**：
  - 於頁籤 1「Step 2：目前月薪」上方加入「快速帶入政府公告各行業薪資中位數」下拉選單。
  - 下方顯示說明小字：`💡 口徑說明：採用行政院主計總處最新公告之「各行業受僱員工全年總薪資中位數 ÷ 12」（已涵蓋年終獎金、加班費與非經常性獎金之月均攤提）。`
  - 選取後自動更新 `#inp-salary`，並顯示目前設定月薪相較於國人總中位數（44,000 元）之比例標籤。

### Task 2.2：階段式（每 5 年／10 年）與逐年自訂月薪輸入模式
- **修改目標**：`prototype/retire_backtest_mvp.html` 頁籤 1「Step 2」與 `backend_mvp/backtest_and_retire_engine.py`
- **規格細節**：
  1. 新增薪資成長模式切換 `#inp-salaryMode`：
     - `fixed_growth`（預設）：依起始月薪 × `(1 + 每年薪資成長率)^yrIdx` 計算。
     - `stepped_5yr`：**每 5 年階段式固定月薪**（自動依 `目前年齡` 至 `預計退休年齡` 每 5 歲切出一個輸入框，例如 `32~36歲`、`37~41歲`、`42~46歲`...，**同一階段區間內維持該設定月薪固定不變**）。
     - `stepped_10yr`：**每 10 年階段式固定月薪**（每 10 歲切出一個輸入框，區間內月薪固定不變）。
     - `custom_yearly`：**展開逐年自訂月薪表**（可針對任一年齡單獨修改月薪）。

### Task 2.3：全週期「額外大筆花費項目」設定模組（出國旅遊、換手機等）
- **修改目標**：`prototype/retire_backtest_mvp.html` 頁籤 1 與 `backend_mvp/backtest_and_retire_engine.py`
- **規格細節**：
  1. 在頁籤 1 新增「Step 3.5：每年／週期性額外大筆花費項目」區塊，預設內建 2 筆範例（可自由勾選啟用、修改金額、刪除或點擊 `+ 新增花費項目`）：
     - 項目 1：`每年固定出國旅遊`，單次金額 `100,000` 元（今日物價），頻率 `每 1 年一次`，生效年齡 `目前年齡 ~ 75 歲`。
     - 項目 2：`定期更換手機/3C`，單次金額 `40,000` 元（今日物價），頻率 `每 5 年一次`，生效年齡 `目前年齡 ~ 90 歲`。
  2. **扣款與通膨計算公式**：
     - 在第 $t$ 年（年齡 $\text{age} = \text{curAge} + t$），若某項目處於生效年齡區間且 $(t \bmod \text{intervalYears} == 0)$，則當年發生該筆額外支出，通膨調整後名目金額為：
       $$E_{\text{extra}, t} = \text{amountToday} \times (1 + \pi)^t$$
     - **全週期扣除（從工作累積期一路扣到退休後）**：
       - 在**工作累積期**：當年度底自投資組合資產中扣除 $E_{\text{extra}, t}$（並於現金流明細表新增「當年額外大筆花費」欄位清楚列出）。
       - 在**退休提領期**：將 $E_{\text{extra}, t}$ 直接加總至當年度的總提領金額中。

### Task 2.4：退休提領時點切換（每年初一次提領 vs. 每月定期提領）
- **修改目標**：`prototype/retire_backtest_mvp.html` 頁籤 1「Step 4」與 `backend_mvp/backtest_and_retire_engine.py`
- **規格細節**：
  1. 新增切換選單 `#inp-withdrawTiming`：
     - `annual_boy`（**每年初一次提領**）：於每年初先把整年度基本生活費＋當年額外大筆花費 $W_{\text{withdraw}, k}$ 從期初資產 $W_k$ 扣除，剩餘本金 $\max(0, W_k - W_{\text{withdraw}, k})$ 再乘上當年度市場漲跌幅與殖利率：
       $$W_{k+1} = \max(0,\; W_k - W_{\text{withdraw}, k}) \times (1 + r_{\text{price}, k} + y_{\text{div}, k})$$
     - `monthly`（**每月定期提領**）：生活費分 12 個月平均從市場提領，尚未提領之資金在年中仍參與約半年的市場漲跌與配息：
       $$W_{k+1} = \max\left(0,\; W_k \times (1 + r_{\text{price}, k} + y_{\text{div}, k}) - W_{\text{withdraw}, k} \times \left(1 + 0.5 \times (r_{\text{price}, k} + y_{\text{div}, k})\right)\right)$$

### Task 2.5：新增第 5 種提領規則——「國人薪資中位數提領法」
- **修改目標**：`prototype/retire_backtest_mvp.html` 頁籤 1「Step 4」與 `backend_mvp/backtest_and_retire_engine.py`
- **規格細節**：
  1. 於 `#inp-withdrawRule` 下拉選單新增第 5 個選項：`rule_median_salary`（**5. 國人薪資中位數提領法**）。
  2. 提供「薪資中位數加減比例」滑桿或輸入框 `#inp-medianRatio`（預設 `100%`，可調範圍 `50% ~ 200%`，例如設 `80%` 代表每月提領國人薪資中位數的 8 成、設 `120%` 代表 1.2 倍）。
  3. **計算公式**：
     - 以政府公告全體受僱員工全年總薪資中位數月均額 $M_{\text{nat}, 0} = 44,000$ 元為今日物價基準。
     - 當使用者選擇此規則時，退休第 $k$ 年（離現在第 $T+k$ 年）之基本年提領金額為：
       $$L_{\text{median}, k} = 12 \times 44,000 \times \left(\frac{\text{medianRatio}}{100}\right) \times (1 + \pi)^{T+k}$$
       再加上當年度到期之額外大筆花費 $E_{\text{extra}, T+k}$。
     - 於「全生命週期財富軌跡圖」新增第 5 條折線（顏色 `#0891b2` 青藍色）：`5. 國人薪資中位數提領法`，與既有 4 種提領法同圖比較！
