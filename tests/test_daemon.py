"""无人值守循环的判据验收。

这里测的不是"能不能跑"，而是"能不能拒"——自主研究最危险的不是写不好，
是**把别的东西当成学到的知识**。2026-10-18 21:05 那次真实事故：
搜 phase change material 命中 Phase Change Memory（电脑存储器），
报告 10/10 通过校验，被当成建筑围护结构的结论入库。
下面每条都是那次事故的直接反例。

    D:\\anaconda3\\python.exe tests\\test_daemon.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DEEPDIVE_RULE_DIR"] = tempfile.mkdtemp(prefix="daemon_test_")

import find_papers as fp  # noqa: E402
import research_daemon as rd  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 核心词

check("核心名词取最后一个实词", fp.head_term("phase change material") == "material",
      fp.head_term("phase change material"))
check("核心名词跳过虚词", fp.head_term("thermal resistance of multilayer wall") == "wall",
      fp.head_term("thermal resistance of multilayer wall"))
check(
    "核心名词跳过通用词（optimization/performance 不能当核心）",
    fp.head_term("heat pump cycle performance optimization") == "cycle",
    fp.head_term("heat pump cycle performance optimization"),
)

# ---------------------------------------------------------------- 标题判据（事故复现）

Q1 = "phase change material thermal energy storage building"
CASES = [
    (
        {
            "title": "Energy Saving Techniques for Phase Change Memory (PCM)",
            "summary": "Phase change memory is a promising non-volatile data storage technology "
                       "with low power consumption and high endurance for computer memory devices.",
        },
        False,
        "同形异义：PCM=电脑存储器",
    ),
    (
        {
            "title": "Room temperature reversible colossal volto-magnetic effect",
            "summary": "We report multiferroic heterostructures with magneto-electric coupling.",
        },
        False,
        "别的学科",
    ),
    (
        {
            "title": "Thermal performance of PCM-integrated building envelopes",
            "summary": "We study phase change material integrated into building envelopes to "
                       "store thermal energy and shift peak cooling loads.",
        },
        True,
        "真相关，标题用缩写但摘要写了全称",
    ),
    (
        {
            "title": "An overview of electric resistance measurement methods",
            "summary": "A review of electric resistance measurement for conductors.",
        },
        False,
        "resistance 撞词",
    ),
]
for paper, want, why in CASES:
    got, reason = fp.paper_matches(paper, Q1)
    check(f"下载前判据：{why}", got is want, f"得到 {got}（{reason}）")

check(
    "带连字符的词拆开也能匹配",
    fp.paper_matches(
        {
            "title": "Self-rewarding correction for mathematical reasoning",
            "summary": "Models learn to correct their own reasoning without external feedback.",
        },
        "self-correction large language model reasoning",
    )[0],
    "self-correction 应能对上 self-rewarding correction",
)

# ---------------------------------------------------------------- 方向隔离

TOPIC = "phase change material thermal energy storage building"
OTHERS = ["heat pump cycle performance optimization", "thermal resistance multilayer wall conduction"]

ok, why = rd.on_topic("phase change material thermal energy storage building", TOPIC, OTHERS)
check("方向内的关键词放行", ok, why)

ok, why = rd.on_topic("heat pump cycle thermal resistance multilayer wall", TOPIC, OTHERS)
check("混入别方向的关键词被拦", not ok, why)

ok, why = rd.on_topic("quantum decoherence measurement", TOPIC, OTHERS)
check("完全无关的关键词被拦", not ok, why)

# 说明这条判据的边界：它只能拦住「已配置的其它方向」的词汇。
# 如果模型凭空引入一个谁都没配置过的学科名词（比如在研究相变储能时
# 写进 self-rewarding），这一步拦不住 —— 那种情况由后面的标题核验和
# 来源核验兜底：标题对不上核心词就不下载，来源对不上就整篇作废。
ok, why = rd.on_topic("phase change material quantum tunneling qubits", TOPIC, OTHERS)
check(
    "本方向词 + 无关学科名词（已知边界：这一步不拦）",
    ok,
    "它会被后面的标题/摘要判据挡掉：搜不到对题论文，或来源核验不过",
)

ok, why = rd.on_topic(
    "phase change material thermal energy storage building nocturnal thermal inertia",
    TOPIC,
    OTHERS,
)
check("本方向内新增两个限定词 → 放行", ok, why)

ok, why = rd.on_topic("phase change material self-rewarding reasoning", TOPIC, OTHERS)
check("混入本方向之外的方法名被拦", not ok, why)

# ---------------------------------------------------------------- 来源对题（第二道防线）

BAD_REPORT = """# 问题

正文。

## Sources

1. `E:\\lib\\Energy Saving Techniques for Phase Change Memory (PCM).pdf`
2. `E:\\lib\\Room temperature colossal volto-magnetic effect.pdf`

## Evidence

**1.** Multiferroic materials are researched for magneto-electric effects.
   - [✓] `E:\\lib\\x.pdf` · p.1
"""

GOOD_REPORT = """# 问题

正文。

## Sources

1. `E:\\lib\\Thermal performance of PCM-integrated building envelopes.pdf`
2. `E:\\lib\\Energy storage in building walls with phase change material.pdf`

## Evidence

**1.** PCM 可以提升围护结构的等效热容。
   - [✓] `E:\\lib\\y.pdf` · p.3
"""

ok, why = rd.sources_on_topic(BAD_REPORT, Q1)
check("引用来源跑题 → 拒绝", not ok, why)

ok, why = rd.sources_on_topic(GOOD_REPORT, Q1)
check("引用来源对题 → 放行", ok, why)

no_src = "# 问题\n\n正文\n\n## Evidence\n\n**1.** x\n"
ok, why = rd.sources_on_topic(no_src, Q1)
check("没有来源列表 → 拒绝", not ok, why)

# ---------------------------------------------------------------- 方向相关的已知事实

facts_scoped, n_scoped, _ = rd.known_state("self-correction large language model reasoning")
check(
    "取事实时只取本方向的（不把 AI 结论喂给建环问题）",
    "multiferroic" not in facts_scoped.lower(),
    facts_scoped[:80],
)

print(f"\n总计：通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
