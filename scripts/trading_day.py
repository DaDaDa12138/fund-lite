#!/usr/bin/env python3
"""交易日闸门：交易日退出 0，非交易日退出 78（EX_CONFIG），供 CI 判定是否跑主流程。

为什么放在脚本里而不是内联：日历逻辑要走 fundana.cn_calendar（含法定节假日表），
内联进 YAML 既难读也没法单测。
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fundana.cn_calendar import is_trading_day  # noqa: E402

EX_CONFIG = 78


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--date":
        today = datetime.date.fromisoformat(args[1])
    else:
        today = datetime.date.today()
    ok = is_trading_day(today)
    print(("交易日 " if ok else "非交易日 ") + today.isoformat())
    return 0 if ok else EX_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
