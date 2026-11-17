set -euo pipefail
cd ~/fund
while pgrep -f "[c]ollect --domain holding" >/dev/null 2>&1; do sleep 20; done
echo "collector finished $(date +%H:%M:%S)"
python3 - <<'PY'
import sqlite3
c=sqlite3.connect('var/db/fundana.db')
print('holdings codes', c.execute('SELECT COUNT(DISTINCT code) FROM src_zqcc_holding').fetchone()[0])
PY
python3 -m fundana fuse --types bond --business-date 2026-10-31
python3 -m fundana screen --profile bond_v1
python3 -m fundana publish
python3 -m fundana verify --fast
python3 scripts/acceptance.py
