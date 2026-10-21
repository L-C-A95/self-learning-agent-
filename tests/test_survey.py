"""成果报告验收：知识点必须挂回原文，判断必须挂回知识点。

这里最要紧的一条：`extract_points()` 的引文**必须来自正文**，
不是模型写的。做法是让模型只报句子编号，引文由程序按编号取——
所以哪怕模型胡报编号，也只会被范围检查挡掉，不会编出一句原文。

    D:\\anaconda3\\python.exe tests\\test_survey.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import survey  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 句子切分

TEXT = (
    "Self-correction without external feedback is unreliable in reasoning tasks. "
    "Our experiments show accuracy drops after self-correction. "
    "The improvement in prior work comes from oracle labels that are unavailable in practice."
)
sents = survey.split_sentences(TEXT)
check("英文断句：切出 3 句", len(sents) == 3, f"{len(sents)} 句")
check("断句保留句号", sents[0].endswith("."), sents[0][-12:])

# ---------------------------------------------------------------- 引文来自原文

# extract_points 会丢掉过短正文（<500 字符），所以补一段够长的正文
FILLER = (
    "We further analyze the failure modes of self-correction across multiple model families "
    "and find that the degradation is consistent when the verifier is the same model that "
    "produced the answer, which suggests the problem is structural rather than a tuning issue. "
)
CHUNKS = [(TEXT + " " + FILLER, 7)]  # 假装这段正文在第 7 页

fake = {
    "points": [
        # 正常：编号在范围内
        {"point_zh": "没有外部反馈时，自我纠正不可靠。", "evidence": [1]},
        # 编号越界：必须丢弃
        {"point_zh": "这条引用了不存在的句子。", "evidence": [99]},
        # 没有编号：必须丢弃
        {"point_zh": "这条根本没有给出依据。"},
        # 太短：必须丢弃
        {"point_zh": "太短", "evidence": [2]},
    ]
}


def fake_ollama(prompt, timeout=0, temperature=0):
    return fake


original = survey.ollama_json
survey.ollama_json = fake_ollama
try:
    got = survey.extract_points({"title": "T", "summary": "S"}, CHUNKS)
finally:
    survey.ollama_json = original

check("只保留 1 条（越界/无编号/过短都被丢）", len(got) == 1, f"{len(got)} 条：{[g['point'][:12] for g in got]}")
if got:
    check(
        "引文逐字取自原文，不是模型写的",
        survey._norm(got[0]["quote"]) in survey._norm(TEXT),
        got[0]["quote"][:60],
    )
    check("页码跟着引文走", got[0]["page"] == 7, str(got[0]["page"]))

# ---------------------------------------------------------------- 方向必须有依据

RAW_DIRECTIONS = {
    "directions": [
        {"direction": "在没有强验证器时改进内部反馈机制", "basis": ["k1"], "kind": "推断",
         "how_to_check": "在 3 个基准上对比有无强验证器"},
        {"direction": "这个方向没有挂任何依据，必须被丢掉", "basis": [], "kind": "推断"},
        {"direction": "引用了不存在的知识点", "basis": ["k99"], "kind": "推断"},
        {"direction": "太短", "basis": ["k1"]},
    ]
}
POINTS = [{"id": "k1", "point": "无外部反馈时自我纠正不可靠", "paper": "p1",
           "quote": "x", "page": 1}]

survey.ollama_json = lambda *a, **k: RAW_DIRECTIONS
try:
    dirs = survey.build_directions(POINTS, "t")
finally:
    survey.ollama_json = original

check("没有依据的方向被丢弃", len(dirs) == 1, f"{len(dirs)} 条")
check("保留的那条挂了依据", dirs and dirs[0]["basis"] == ["k1"], str(dirs))

# ---------------------------------------------------------------- 可实践性线索

check("识别出代码线索", "有代码/数据线索" in survey.practice_signal("code is available on github"))
check("识别出实验线索", "有实验或仿真" in survey.practice_signal("we measured the temperature"))
check("没有线索时如实说", survey.practice_signal("nothing here") == "未见代码/实验线索")

# ---------------------------------------------------------------- 标题截断

check("截断不切在单词中间", not survey.short_title("Self-Correcting Reasoning Models", 12).rstrip("…").endswith("-"),
      survey.short_title("Self-Correcting Reasoning Models", 12))
check("短标题不截断", survey.short_title("Short title") == "Short title")

# ---------------------------------------------------------------- 渲染

REPORT = survey.render(
    "测试主题",
    [{"id": "p1", "title": "Paper One", "year": "2024", "signal": "有代码/数据线索"}],
    POINTS + [{"id": "k2", "point": "第二条", "paper": "p1", "quote": "y", "page": 3}],
    [{"name": "同一问题", "ids": ["k1", "k2"], "differs": "一个用外部反馈，一个用内部反馈"}],
    [{"direction": "往 A 走", "basis": ["k1"], "kind": "推断", "check": "做个实验"}],
)
for section in ("一、知识点总结", "二、横向对比", "三、纵向演进", "四、值得往哪走", "五、这份报告没做到的"):
    check(f"报告含「{section}」", section in REPORT)
check("报告标注了推断与结论的区别", "推断" in REPORT and "论文结论" in REPORT)

print(f"\n总计：通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
