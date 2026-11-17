"""配置加载：app.yaml + source_registry.yaml + field_policy.yaml，并计算 config_hash。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class SourceSpec:
    source_id: str
    domain: str
    url: str
    encoding: str = "utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    ttl_days: float = 1.0
    rate_qps: float = 2.0
    parser: str = ""
    method: str = "GET"
    note: str = ""


@dataclass
class Config:
    app: dict[str, Any]
    sources: dict[str, SourceSpec]
    field_policy: dict[str, Any]
    raw: dict[str, Any]
    config_hash: str
    root: Path
    registry_version: str = ""

    # ── 便捷访问 ──
    @property
    def db_path(self) -> Path:
        return self.root / self.app["db"]["path"]

    @property
    def raw_dir(self) -> Path:
        return self.root / self.app["paths"]["raw_dir"]

    @property
    def freeze_dir(self) -> Path:
        return self.root / self.app["paths"]["freeze_dir"]

    @property
    def report_root(self) -> Path:
        return self.root / self.app["paths"]["report_root"]

    @property
    def code_version(self) -> str:
        return str(self.app["app"]["code_version"])

    @property
    def timezone(self) -> str:
        """项目固定时区；业务日/时间戳/调度都按这个时区解释。"""
        return str((self.app.get("app") or {}).get("timezone") or "Asia/Shanghai")

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.app
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"配置格式错误（期望映射）: {path}")
    return data


def load_config(root: Path | None = None) -> Config:
    root = Path(root) if root else ROOT
    cfg_dir = root / "config"
    app = _read_yaml(cfg_dir / "app.yaml")
    tz = str((app.get("app") or {}).get("timezone") or "Asia/Shanghai")
    if tz != "Asia/Shanghai":
        raise SystemExit(
            "Fund-ANA 全程固定使用 Asia/Shanghai；"
            f"config/app.yaml 的 app.timezone 当前为 {tz!r}，请改回 Asia/Shanghai。"
        )
    reg = _read_yaml(cfg_dir / "source_registry.yaml")
    pol = _read_yaml(cfg_dir / "field_policy.yaml")

    sources: dict[str, SourceSpec] = {}
    for sid, spec in (reg.get("sources") or {}).items():
        sources[sid] = SourceSpec(
            source_id=sid,
            domain=spec.get("domain", ""),
            url=spec["url"],
            encoding=spec.get("encoding", "utf-8"),
            headers={k: str(v) for k, v in (spec.get("headers") or {}).items()},
            ttl_days=float(spec.get("ttl_days", 1)),
            rate_qps=float(spec.get("rate_qps", 2)),
            parser=spec.get("parser", sid),
            method=spec.get("method", "GET"),
            note=spec.get("note", ""),
        )

    # config_hash 只覆盖"影响结果"的键（不含 api.port 等运维项）
    material = {
        "universe": app.get("universe"),
        "risk": app.get("risk"),
        "quality": app.get("quality"),
        "quality_profile": app.get("quality_profile"),
        "screen": app.get("screen"),
        "publish": app.get("publish"),
        "field_policy": pol,
        "sources": {k: {"url": v.url, "ttl": v.ttl_days, "parser": v.parser} for k, v in sources.items()},
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return Config(
        app=app,
        sources=sources,
        field_policy=pol,
        raw=material,
        config_hash=hashlib.sha256(blob).hexdigest(),
        root=root,
        registry_version=str(reg.get("version", "")),
    )
