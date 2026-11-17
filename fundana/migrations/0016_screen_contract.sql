-- 0016：把筛选契约（thresholds.SCREEN_CONTRACT）落库。
-- 此前 screen_run/screen_batch 只存 metrics 的 formula_contract（fundana-risk-*），
-- 筛选口径升级（v3→v4）在库里没有任何痕迹：既无法判断两次发布的换手是"换了口径"
-- 还是"换了基金"，也无法在事后复现某次榜单用的是哪套阈值（ADR-067）。
ALTER TABLE screen_run ADD COLUMN screen_contract TEXT;
ALTER TABLE screen_batch ADD COLUMN screen_contract TEXT;
