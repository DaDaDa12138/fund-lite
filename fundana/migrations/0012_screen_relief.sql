-- 缓释证据：即使未放行也要留痕（"检查过但没放行"与"没检查"必须可区分）
ALTER TABLE screen_decision ADD COLUMN relief_json TEXT;
