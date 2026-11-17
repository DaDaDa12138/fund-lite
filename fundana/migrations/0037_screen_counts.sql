-- P0：screen_run 拆分 L9 通过 / 正式发布 / 折叠原因。
-- 旧 formal_count 同时被当作 L9 与发布数，报告和 gate 无法区分
-- “通过准入但被 family/company/配额/双榜比例折叠”与真正的门槛失败。
ALTER TABLE screen_run ADD COLUMN l9_pass_count INTEGER;
ALTER TABLE screen_run ADD COLUMN published_count INTEGER;
ALTER TABLE screen_run ADD COLUMN board_trim_json TEXT;
UPDATE screen_run SET l9_pass_count = formal_count WHERE l9_pass_count IS NULL;
