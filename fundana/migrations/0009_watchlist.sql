-- 0009: 自选（收藏）表。只存"用户想长期观察的基金代码 + 备注"，
-- 不存任何行情快照：页面展示时实时 JOIN fund/fund_profile/fund_metric，
-- 这样收藏列表永远跟着最新一次采集走，不会因为旧快照而与榜单口径冲突。
CREATE TABLE IF NOT EXISTS fund_watchlist (
    fcode     TEXT PRIMARY KEY,
    added_at  TEXT NOT NULL,
    source    TEXT,
    note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_watchlist_added ON fund_watchlist(added_at);
