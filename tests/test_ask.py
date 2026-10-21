"""ask.py 的判据测试。不调模型，只测检索、扩展、打散、渲染这些逻辑层。"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ask  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [OK] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 问句解析

print("\n== 问句里的废话要剔掉 ==")
terms = ask.key_terms("我学过什么关于自我纠正的")
check("剔掉'我学过什么关于'", "自我纠正" in terms, str(terms))
check("不把整句当种子", "我学过什么关于自我纠正的" not in terms, str(terms))

terms = ask.key_terms("共识算法的通信复杂度是多少")
check("共识被识别", any("共识" in t for t in terms), str(terms))
check("通信复杂度被识别", any("通信复杂度" in t for t in terms), str(terms))

print("\n== 同义词展开 ==")
terms = ask.key_terms("自我纠正")
check("纠正→修正", "修正" in terms, str(terms))
terms = ask.key_terms("量子比特")
check("量子比特→qubit", "qubit" in terms, str(terms))
check("空问句不炸", ask.key_terms("") == [])
check("纯语气词不炸", ask.key_terms("吗呢吧") != None)

# ---------------------------------------------------------------- 主题打散

print("\n== 按主题打散，防止霸屏 ==")


def _fact(fid: str, score: float, subject: str, action: str = "内容") -> dict:
    return {
        "id": fid,
        "score": score,
        "kind": "fact",
        "trigger": subject,
        "action": action,
        "evidence": "",
        "source": "https://arxiv.org/abs/0000.00000",
        "page": 1,
    }


# 12 条同一个主题 + 1 条别的主题。打散后不能全是第一个主题的。
facts = [_fact(f"a{i}", 0.9 - i * 0.01, "主题甲") for i in range(12)]
facts.append(_fact("b1", 0.2, "主题乙", "另一主题的内容"))
spread = ask._spread(facts, 6)
subs = {f["trigger"] for f in spread}
check("打散后出现第二个主题", "主题乙" in subs, str([f["id"] for f in spread]))
check("总数仍是 k", len(spread) == 6, str(len(spread)))
check("高分优先", spread[0]["id"] == "a0", spread[0]["id"])
check("不超过 k 时原样返回", len(ask._spread(facts[:4], 6)) == 4)

print("\n== 配额用不完时按分数补足 ==")
few = [_fact(f"c{i}", 0.5 - i * 0.01, "主题丙") for i in range(3)]
picked = ask._spread(few + facts, 8)
check("补足到 k", len(picked) == 8, str(len(picked)))
check("补的也是原列表里的", all(f in picked for f in few))

# ---------------------------------------------------------------- 检索合并

print("\n== 检索：原问题 + 变体合并 ==")
real = ask._retrieve("共识算法的通信复杂度", 12)
check("真库能召回到东西", len(real) > 0, f"{len(real)} 条")
check("编号唯一", len({f['id'] for f in real}) == len(real))
check("分数降序", all(real[i]["score"] >= real[i + 1]["score"] for i in range(len(real) - 1)))

solo = ask._retrieve("zzz完全不存在的词xyzzy", 12)
check("问不存在的东西返回空", solo == [], f"{len(solo)} 条")

# ---------------------------------------------------------------- 渲染

print("\n== 渲染：链接和页码必须在 ==")
result = {
    "question": "共识的通信复杂度",
    "answer": "全连接是 O(N²)[1]。",
    "used": [1],
    "facts": [_fact("x1", 0.9, "共识", "全连接共识的通信复杂度为 O(N²)")],
    "terms": [],
    "gap": "",
}
result["facts"][0]["evidence"] = "The communication complexity is O(N^2)."
result["facts"][0]["source"] = "https://arxiv.org/abs/2510.05504v1"
result["facts"][0]["page"] = 3
md = ask.render(result)
check("有问题标题", "# 问：共识的通信复杂度" in md)
check("有回答", "全连接是 O(N²)[1]" in md)
check("有 arXiv 链接", "https://arxiv.org/abs/2510.05504v1" in md)
check("有页码", "p.3" in md)
check("有原文引文", "communication complexity" in md)
check("有依据小标题", "## 依据" in md)

print("\n== 空结果不许硬答 ==")
empty = {
    "question": "不存在的东西",
    "answer": "",
    "used": [],
    "facts": [],
    "terms": [],
    "gap": "知识库里没检索到相关的事实。",
}
md = ask.render(empty)
check("明说没答上", "没答上" in md)
check("没有依据小标题", "## 依据" not in md)

print("\n== 空问题直接返回，不写文件 ==")
check("空问题无回答", ask.answer("")["answer"] == "")
check("空问题不写文件", ask.save({"question": ""}) is None)

print("\n== 存盘 ==")
with tempfile.TemporaryDirectory() as tmp:
    old = ask.QA_DIR
    try:
        ask.QA_DIR = Path(tmp)
        p = ask.save(result)
        check("文件写出", p is not None and p.exists())
        check("问答记录目录名", "问答记录" in str(p) or "问答_" in p.name, str(p))
        check("文件有内容", p.stat().st_size > 200)
    finally:
        ask.QA_DIR = old

print(f"\n总计：通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
