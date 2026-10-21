"""规则库验收：入库闸门、残句过滤、检索、去重、质检分档。

跑法：
    D:\\anaconda3\\python.exe tests\\test_rules.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

# 用临时库，别碰真实规则库
_TMP = tempfile.mkdtemp(prefix="rules_test_")
os.environ["DEEPDIVE_RULE_DIR"] = _TMP

import vector_kb as kb  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 残句过滤

NOISE = [
    ("The correct answer is 24.", "例题残句"),
    ("Baseline: improving the self-correction ability.", "表格残句"),
    ("| a | b | c |", "markdown 表格"),
    ("## Key findings", "标题"),
    ("Single-turn STaR 77.0 - - -", "表格碎片"),
    ("答案：24", "答案残句"),
]
for text, label in NOISE:
    check(f"残句被拒：{label}", kb._is_noise(text) is not None, f"实际通过：{text[:40]}")

GOOD = [
    "多层平壁的总热阻等于各层热阻之和，这是串联热阻的基本结论。",
    "self-correction naturally emerges during RL training in large reasoning models.",
]
for text in GOOD:
    check(f"正常结论保留：{text[:24]}…", kb._is_noise(text) is None, str(kb._is_noise(text)))


# ---------------------------------------------------------------- Evidence 解析

REPORT = """# 测试问题

## Background

这一段是正文，不允许进库。

## Sources

1. `E:\\lib\\a.pdf`

## Evidence

_2/3 claims are span-grounded (verbatim excerpt verified against source)._

**1.** 多层平壁的总热阻等于各层热阻之和，这是串联热阻的基本结论。
   - [✓] `E:\\lib\\a.pdf` · p.12
     > 多层平壁的总热阻等于各层热阻之和。

**2.** The correct answer is 24.
   - [✓] `E:\\lib\\a.pdf` · p.5
     > This is 125 − 101 = 24.

**3.** 这一条没有通过校验，不该入库的任何位置。
   - [✗] `E:\\lib\\a.pdf` · p.9
     > （无）
"""

tmp_report = Path(_TMP) / "r.md"
tmp_report.write_text(REPORT, encoding="utf-8")

blocks = kb.parse_evidence(REPORT)
check("Evidence 解析出 3 块", len(blocks) == 3, f"{len(blocks)} 块")
check("正文没被误当证据", all("这一段是正文" not in b["text"] for b in blocks))
check("页码被解析出来", blocks[0]["cites"][0]["page"] == 12, str(blocks[0]["cites"]))

stats = kb.import_report(tmp_report, "测试主题", force=True)
check("只入库 1 条（残句与未校验都排除）", stats["入库"] == 1, str(stats))
check("跳过原因可追溯", stats["跳过"] == 2, str(stats))


# ---------------------------------------------------------------- 质检分档

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import qc_gate  # noqa: E402

local_report = """# 问题

正文内容，用来凑字符数。这里是教材库的回答，来源只有一个 PDF。
""" + "内容。" * 120 + """

## Sources

1. `E:\\lib\\传热学.pdf`

## Evidence

**1.** 傅里叶定律描述热流密度与温度梯度的关系，是导热分析的基础。
   - [✓] `E:\\lib\\传热学.pdf` · p.87
     > 傅里叶定律描述热流密度与温度梯度的关系。
"""
p = Path(_TMP) / "local.md"
p.write_text(local_report, encoding="utf-8")
ok, msg = qc_gate.check_report_quality(str(p))
check("本地库报告：1 个来源也算通过", ok, msg)

web_report = local_report.replace("`E:\\lib\\传热学.pdf`", "[某网页](https://a.example/x)")
p2 = Path(_TMP) / "web.md"
p2.write_text(web_report, encoding="utf-8")
ok2, msg2 = qc_gate.check_report_quality(str(p2))
check("网页报告：1 个来源仍被拒", not ok2, msg2)


# ---------------------------------------------------------------- 检索与去重

hits = kb.search("多层平壁总热阻怎么算")
check("检索能命中已存事实", bool(hits) and hits[0]["kind"] == "fact", str(hits[:1]))
check("检索带出处与页码", bool(hits) and hits[0]["page"] == 12, str(hits[:1]))

miss = kb.search("量子计算机退相干时间如何测量")
check("无关问题不返回结果", not miss, str(miss[:1]))

kb.add_rule("写中文报告", "正文必须全中文，禁止中英混排")
again = kb.add_rule("写中文报告", "正文必须全中文，禁止中英混排")
check("同一条规则不会重复入库", again["入库"] == 0, str(again))

draft = kb.check("这份报告的正文必须全中文。")
check("草稿检查能命中规则", bool(draft), str(draft))

# 撤销后不再命中
rule_id = kb.add_rule("测试撤销", "这条规则应该可以被撤销掉")["id"]
import sqlite3  # noqa: E402

conn = sqlite3.connect(kb.DB_PATH)
conn.execute("UPDATE rules SET status='retired' WHERE id=?", (rule_id,))
conn.commit()
conn.close()
check(
    "撤销后不再出现在检索结果里",
    all(r["id"] != rule_id for r in kb.search("这条规则应该可以被撤销掉", k=10)),
)

print(f"\n总计：通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
