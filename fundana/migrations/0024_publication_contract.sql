-- 0024：发布记录落筛选/指标契约，便于审计“同一批次是否换了口径”。
-- screen_batch 已有 screen_contract；publication 之前只存 run_id，事后无法判断。
ALTER TABLE publication ADD COLUMN screen_contract TEXT;
ALTER TABLE publication ADD COLUMN formula_contract TEXT;
