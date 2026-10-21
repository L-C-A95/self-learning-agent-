"""自主浏览的两条判据：学科边界、跨论文关系句是否真的在说关系。

2026-10-19 真实事故：方向写着"经济与博弈"，它跑去读量子点光催化；
以及关系句写成"P1 研究了 A，P2 研究了 B"——那是复述，不是关系。

    D://anaconda3//python.exe tests\\test_explore.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import explore  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 学科边界

check(
    "本学科的检索词通过",
    explore.on_field("phase change material thermal storage", "phase change material thermal energy storage"),
)
check(
    "跑题的检索词被拦（量子点 ≠ 经济与博弈）",
    not explore.on_field("perovskite quantum dots charge transfer", "mechanism design auction"),
)
check("空的检索词不算通过", not explore.on_field("", "mechanism design auction"))

# ---------------------------------------------------------------- 关系句

GOOD = [
    "P1 证明 A 在常温下有效，但 P2 发现超过 300K 后失效，适用边界不同",
    r"两篇都得到 \(14%\) 的提升，互相印证",
    "P2 补上了 P1 的缺口：P1 只做仿真，P2 有实测数据",
]
BAD = [
    "P1 研究了 A，P2 研究了 B",
    "P1 研究大尺度相干结构，P2 通过水平穿越分析揭示时间尺度特征",
    "P1 提出了一种方法，P2 讨论了应用",
]
for s in GOOD:
    check(f"关系句保留：{s[:20]}…", explore._is_real_relation(s), "被误杀")
for s in BAD:
    check(f"复述句丢弃：{s[:20]}…", not explore._is_real_relation(s), "被放过")

print(f"\n总计：通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
