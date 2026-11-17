-- 0023: 记录每轮筛选使用的排序 profile 与评估协议版本（ADR-084）。
-- 不改变既有 run 的语义；旧行留空，表示“生产 current、协议未标注”。
ALTER TABLE screen_run ADD COLUMN ranking_profile TEXT;
ALTER TABLE screen_run ADD COLUMN protocol_version TEXT;
