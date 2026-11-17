-- 0015: 风格标签历史快照，让回测与复盘能用 as-of 标签而不是当前标签（ADR-065）
CREATE TABLE IF NOT EXISTS fund_style_history(
  fcode              TEXT NOT NULL,
  as_of_date         TEXT NOT NULL,
  style_code         TEXT NOT NULL,
  confidence         TEXT NOT NULL,
  classifier_version TEXT,
  created_at         TEXT,
  PRIMARY KEY (fcode, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_style_history_date ON fund_style_history(as_of_date);
