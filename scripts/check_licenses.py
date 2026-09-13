"""License 合规扫描（13-发布合规核对清单 §二；CI audit job 与本地共用）。

扫描当前 venv 已安装包的 METADATA（License / License-Expression 字段）：
- 命中 Copyleft/NC 模式（GPL/LGPL/AGPL/SSPL/EUPL/CC-BY-NC）且不在白名单 → 退出码 1；
- 白名单为已留痕接受的组件（决策记录见 docs/13 §二）；
- METADATA 缺 License 字段的包仅告警（上游未声明，不阻断）。
"""

from __future__ import annotations

import email.parser
import re
import sys
import sysconfig
from pathlib import Path

# 只匹配"声明头部"的结构化 Copyleft 表达（见 main 中的 HEAD 截断）：
# 裸词 "GPL" 与全称在部分包的内嵌历史 license 长文里大量出现，不能作为判定依据
DENY = re.compile(
    r"(GPLv?\s?[-\s]?[23](\.\d+)?"
    r"|GNU (Lesser |Affero )?General Public License"
    r"|\bSSPL|\bEUPL|CC-BY-NC)",
    re.I,
)

# License 声明（SPDX 表达式或全称）位于字段头部；长文正文（内嵌条款全文）不参与判定
HEAD_CHARS = 200

# 已留痕接受的组件（决策与理由见 docs/13-发布合规核对清单.md §二）
ALLOWLIST: dict[str, str] = {
    "igraph": "可选 communities extra（Leiden 加速），未安装时降级 networkx；2026-09-14 决策接受",
    "leidenalg": "同 igraph（Leiden 依赖）；2026-09-14 决策接受",
}


def _licenses() -> list[tuple[str, str]]:
    site = Path(sysconfig.get_paths()["purelib"])
    rows: list[tuple[str, str]] = []
    for dist in sorted(site.glob("*.dist-info")):
        msg = email.parser.BytesParser().parsebytes((dist / "METADATA").read_bytes())
        name = str(msg["Name"] or dist.name.removesuffix(".dist-info"))
        lic = str(msg["License"] or "").strip()
        expr = msg.get_all("License-Expression") or []
        if expr:
            lic = str(expr[0])
        rows.append((name, lic.replace("\n", " ")))
    return rows


def main() -> int:
    violations: list[str] = []
    unknown: list[str] = []
    for name, lic in _licenses():
        if not lic:
            unknown.append(name)
            continue
        if DENY.search(lic[:HEAD_CHARS]) and name not in ALLOWLIST:
            violations.append(f"{name}: {lic[:120]}")
    if unknown:
        print(f"[warn] METADATA 未声明 License（不阻断）: {', '.join(unknown)}")
    if violations:
        print("[error] 命中 Copyleft/NC License 且未在白名单留痕：")
        for v in violations:
            print(f"  - {v}")
        print(
            "新增依赖请在 docs/13 §二登记并核对 License；确需引入 Copyleft 组件请更新白名单并留痕。"
        )
        return 1
    print("[ok] License 扫描通过（白名单外无 Copyleft/NC 组件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
