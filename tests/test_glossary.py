"""术语解析的几条判据：词得在原文里、解释不能是术语换术语。

2026-10-19 真实事故：让模型讲「BFT」，它回"BFT 是一种容错机制"——
把词再说一遍，读者还是不懂；还有一次它自己加了原文里没有的词。

    D://anaconda3//python.exe tests\\test_glossary.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import glossary as gl  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 词必须在原文里

BLOB = "崩溃容错（CFT）协议如 Raft 和 Paxos 用于管理可靠状态复制；全连接共识的通信复杂度为 O(N2)"

check("原文出现的缩写命中", gl._on_page("CFT", BLOB))
check("原文出现的算法名命中", gl._on_page("Raft", BLOB))
check("原文出现的中文术语命中", gl._on_page("通信复杂度", BLOB))
check("模型自造的词被拦", not gl._on_page("Paxos 变体", BLOB) or "paxos变体" not in BLOB.lower())
check("原文没有的词被拦（量子纠缠）", not gl._on_page("量子纠缠", BLOB))
check("单字不算术语", not gl._on_page("的", BLOB))

# ---------------------------------------------------------------- 解释得有信息

GOOD = {
    "term": "CFT",
    "plain": "即使有机器彻底死机不再说话，整套系统还能照常干活，就像停电时有备用电源顶着",
}
BAD_SHORT = {"term": "CFT", "plain": "一种容错机制"}
BAD_LOOP = {"term": "CFT", "plain": "CFT 是一种容错机制"}
BAD_DEFINE = {"term": "共识", "plain": "是指多个节点达成一致"}

check("带比方的白话解释保留", gl._keep(GOOD, BLOB), "被误杀")
check("太短的糊弄解释丢弃", not gl._keep(BAD_SHORT, BLOB), "被放过")
check("把词再说一遍的丢弃", not gl._keep(BAD_LOOP, BLOB), "被放过")
check("纯定义式丢弃", not gl._keep(BAD_DEFINE, BLOB), "被放过")

# ---------------------------------------------------------------- 英文词抽取

MUST = gl.must_terms("Hotstuff 协议相比其他 BFT 协议具有更低的通信复杂度；崩溃容错（CFT）如 Raft")
check("抽出缩写与算法名", {"Hotstuff", "BFT", "CFT", "Raft"} <= set(MUST), str(MUST))
check("O(N2) 拆出来的 N2 不算术语", "N2" not in gl.must_terms("通信复杂度为 O(N2)"))
check("D-CAST 连字符不拆开", "D-CAST" in gl.must_terms("D-CAST 被提出来应对故障"), str(gl.must_terms("D-CAST 被提出来应对故障")))
check("英文词最多取 4 个（够用就行）", len(gl.must_terms("Aaa Bbb Ccc Ddd Eee Fff")) <= 4)

# ---------------------------------------------------------------- 收尾

print(f"\n总计：通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
