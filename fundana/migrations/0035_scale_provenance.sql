-- P0-2：规模历史逐行溯源 + fetch_log 按 (source,key) 回填索引。
-- 背景：fund_scale_history 之前只写 src='em_gmbd'，没有 raw_sha256，无法反查到 raw_object。
-- 最新一次 em_gmbd 原始响应包含该基金全部季度，因此历史各期共用同一 raw_sha256 是可接受的。
ALTER TABLE fund_scale_history ADD COLUMN raw_sha256 TEXT;

CREATE INDEX IF NOT EXISTS idx_fetch_log_source_key
    ON fetch_log(source_id, key, started_at DESC);

UPDATE fund_scale_history
SET raw_sha256 = (
    SELECT f.raw_sha256
    FROM fetch_log f
    WHERE f.source_id = 'em_gmbd'
      AND f.key = fund_scale_history.fcode
      AND f.business_ok = 1
      AND f.raw_sha256 IS NOT NULL
    ORDER BY f.started_at DESC
    LIMIT 1
)
WHERE raw_sha256 IS NULL;
