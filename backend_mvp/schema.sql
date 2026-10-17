-- ============================================================================
-- RetirePilot 跨市場（台股／美股）20年回測與退休模擬系統 — 資料庫 Schema (v1.2)
-- 相容於 PostgreSQL / TimescaleDB / SQLite / DuckDB
-- 新增：真實上市年份 (inception_year) 與嚴格白名單代理母標的 (proxy_ticker, proxy_leverage)
-- ============================================================================

-- 1. 標的清單表 (儲存台股百大市值、台股百大ETF、美股百大市值、美股百大ETF 共 400 檔核心標的)
CREATE TABLE IF NOT EXISTS instruments (
    ticker VARCHAR(20) PRIMARY KEY,            -- 股票代號，例：'2330.TW', '0050.TW', '00631L.TW', 'NVDA', 'VOO'
    name_zh VARCHAR(100) NOT NULL,             -- 中文名稱：台積電、元大台灣50、元大台灣50正2、輝達
    name_en VARCHAR(150),                      -- 英文名稱
    market VARCHAR(10) NOT NULL,               -- 市場別：'TW' 或 'US'
    category VARCHAR(30) NOT NULL,             -- 分類：'TW_TOP100_CAP', 'TW_TOP100_ETF', 'US_TOP100_CAP', 'US_TOP100_ETF'
    currency VARCHAR(5) NOT NULL,              -- 計價幣別：'TWD' 或 'USD'
    withholding_tax_rate NUMERIC(5,4) DEFAULT 0.0, -- 股息預扣稅率：美股預設 0.3000 (30%)，台股預設 0.0000
    inception_year INT DEFAULT 2006,           -- 真實掛牌上市年份 (供「嚴格真實上市日截斷」與「短樣本貝氏收斂」使用)
    proxy_ticker VARCHAR(20) DEFAULT NULL,     -- 嚴格白名單代理母標的 (例：006208->0050.TW, 00631L->0050.TW, VOO->SPY；無替代指數之C級標的為 NULL)
    proxy_leverage NUMERIC(5,2) DEFAULT 1.0,   -- 代理槓桿倍數 (例：A級雙胞胎ETF為 1.0，B級正2為 2.0、正3為 3.0)
    is_active BOOLEAN DEFAULT TRUE,            -- 是否仍在市交易
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 每日價格與還原權值表 (400 檔 x 20 年約 200 萬筆紀錄)
CREATE TABLE IF NOT EXISTS daily_prices (
    ticker VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    open_price NUMERIC(14,4),
    high_price NUMERIC(14,4),
    low_price NUMERIC(14,4),
    close_price NUMERIC(14,4) NOT NULL,        -- 原始收盤價 (供計算實際買入股數、當期殖利率與領息現金流)
    adj_close NUMERIC(14,4) NOT NULL,          -- 還原收盤價 (已調整除權息與拆併股，供快速計算累積總報酬)
    split_factor NUMERIC(10,6) DEFAULT 1.0,    -- 拆併股因子 (例：1拆10 記錄為 10.0)
    volume BIGINT,
    PRIMARY KEY (ticker, trade_date),
    FOREIGN KEY (ticker) REFERENCES instruments(ticker)
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_date ON daily_prices(trade_date);

-- 3. 歷年除權息紀錄表 (支援「股息再投入 DRIP」與「股息領現」精確模擬)
CREATE TABLE IF NOT EXISTS corporate_dividends (
    ticker VARCHAR(20) NOT NULL,
    ex_date DATE NOT NULL,                     -- 除權息日
    pay_date DATE,                             -- 股利發放日 (DRIP 於發放日按當日收盤價買入)
    cash_dividend NUMERIC(12,6) DEFAULT 0.0,   -- 每股現金股利 (原幣)
    stock_dividend NUMERIC(12,6) DEFAULT 0.0,  -- 台股特有：每股股票股利 (盈餘配股元數，如配 0.5 元代表每股無償配發 0.05 股)
    PRIMARY KEY (ticker, ex_date),
    FOREIGN KEY (ticker) REFERENCES instruments(ticker)
);

-- 4. 每日匯率表 (供台美股跨市場組合統一以新台幣 TWD 結算回測)
CREATE TABLE IF NOT EXISTS fx_rates (
    rate_date DATE PRIMARY KEY,
    usd_twd NUMERIC(10,4) NOT NULL             -- 美元兌新台幣收盤匯率
);
