"""前端契约自检：指标字典、排序白名单、数据库列三方对齐。"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    path = ROOT / "scripts" / "check_ui_contract.py"
    spec = importlib.util.spec_from_file_location("check_ui_contract", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class UiContractTest(unittest.TestCase):
    def test_contract_is_clean_on_migrated_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "contract.db"
            conn = sqlite3.connect(db_path)
            for sql in sorted((ROOT / "fundana" / "migrations").glob("*.sql")):
                conn.executescript(sql.read_text(encoding="utf-8"))
            conn.commit()
            conn.close()
            checker = _load_checker()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = checker.check(db_path)
            self.assertEqual(rc, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
