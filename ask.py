"""问答入口：把知识库里学到的东西，用一句人话答出来。

用法：
    python ask.py "共识算法的通信复杂度"     # 单次提问
    python ask.py                            # 交互式，连续问
    python ask.py --dry "..."                # 只看命中了什么，不调模型

设计原则（跟术语解析同一条）：**模型只负责组织语言，事实全部由程序从库里取**。
模型答完要报它用了哪几条事实的编号，程序照编号把原文引文和 arXiv 链接
列在下面。编号对不上是看得见的失败，比它编一个链接好。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import glossary as gl  # noqa: E402
import survey as sv  # noqa: E402
import vector_kb as kb  # noqa: E402

WORK = Path(__file__).resolve().parent
QA_DIR = WORK / "产出" / "问答记录"

# 问句里的废话。不先剔掉，"我学过什么关于自我纠正的"这种问法会被
# 学过/什么/关于 这些 token 把相似度稀释得只剩个位数。
_Q_STOP = {
    "我学", "学过", "学习", "什么", "关于", "知道", "告诉", "讲", "说说", "一下",
    "吗", "呢", "吧", "有没", "有没有", "哪些", "多少", "是不是", "请", "帮",
    "查", "看", "找", "的", "了", "着", "和", "与", "及", "或",
}

# 学术词同义词。库里三种写法并存（自我纠正/自我修正/自纠正循环），
# 二元组分词后只共享"自我"两个字——69 条同主题知识被切成孤岛。
# 这个表宁缺勿滥：只收在 arXiv 摘要里确实混用的，不收口语近义词。
_SYN = {
    "纠正": ["纠正", "修正", "矫正", "纠错"],
    "修正": ["修正", "纠正"],
    "自我": ["自我", "内在"],
    "自反馈": ["自反馈", "反馈", "回馈"],
    "共识": ["共识", "一致"],
    "通信复杂度": ["通信复杂度", "通信开销"],
    "复杂度": ["复杂度", "开销"],
    "量子比特": ["量子比特", "量子位", "qubit"],
    "大模型": ["大模型", "大语言模型", "large", "language", "model"],
    "推理": ["推理", "推断", "reasoning"],
    "神经网络": ["神经网络", "network"],
    "流体": ["流体", "fluid", "flow"],
    "表面码": ["表面码", "surface", "code"],
    "训练": ["训练", "training"],
    "泛化": ["泛化", "generalization"],
    "预测": ["预测", "forecast", "prediction"],
    "扩散": ["扩散", "diffusion"],
    "优化": ["优化", "optimization"],
}

_CJK = re.compile(r"[\u4e00-\u9fff]{2,}")
_LATIN = re.compile(r"[A-Za-z][A-Za-z\-]{1,}")


def key_terms(question: str, limit: int = 3) -> list[str]:
    """从问句里抠出核心词，再按同义词表展开成变体。

    "我学过什么关于自我纠正的" → 自我纠正、自我修正、自纠正、内在纠正
    "共识算法的通信复杂度是多少" → 共识、一致、通信复杂度、通信开销
    """
    text = question.lower()
    for w in sorted(_Q_STOP, key=len, reverse=True):
        text = text.replace(w, " ")

    seeds: list[str] = []
    for run in _CJK.findall(text):
        # 连续中文串整段算一个种子；短于 2 字的不要
        if run not in _Q_STOP:
            seeds.append(run)
    seeds += [w.lower() for w in _LATIN.findall(text) if len(w) >= 3]

    out: list[str] = []
    for seed in seeds[:limit]:
        out.append(seed)
        for k, variants in _SYN.items():
            if k in seed:
                for v in variants:
                    if v not in out:
                        out.append(v)
    return out

_PROMPT = """你在回答一个读者的问题。**只能依据下面这些从论文里摘出来的事实**，
不准用你自己的知识补充，不准提"根据提供的材料"这种话。

事实（编号从 1 开始）：
{facts}

读者的问题：{question}

要求：
1. answer：直接回答，200~400 字，大白话，不要论文腔。用到哪条事实就在句末标 [n]。
2. 如果这些事实不足以回答，在 gap 里说明缺什么（比如"库里有通信复杂度的对比，
   但没有 2025 年之后的进展"），answer 照能答的部分答，不要硬编。
3. 事实之间矛盾的，指出来，不要挑顺嘴的说。

只输出 JSON：{{"answer":"...","used":[1,2],"gap":"..."}}"""


def _retrieve(question: str, k: int) -> list[dict]:
    """原问题 + 同义词变体各搜一遍，按事实 id 合并，取分数最高的前 k 条。

    只搜原问题会漏：问"自我纠正"，库里 41 条叫"自纠正循环"、20 条叫
    "自我修正"，二元组分词后只共享"自我"两字，不展开就召不回来。
    """
    merged: dict[str, dict] = {}
    for term in [question] + key_terms(question):
        for f in kb.search(term, k=8):
            old = merged.get(f["id"])
            if old is None or f["score"] > old["score"]:
                merged[f["id"]] = f
    out = sorted(merged.values(), key=lambda f: -f["score"])
    return _spread(out, k)


def _spread(facts: list[dict], k: int) -> list[dict]:
    """按 subject 打散后再截断。

    直接按分数取前 k 条，同一个主题会霸屏：问"自我纠正"，41 条叫
    "自纠正循环"的因为用词不完全一致，分数全排在 20 条"自我修正"后面，
    一刀切下去就只剩一个主题——那 69 条照样没被放在一起看过。
    """
    if len(facts) <= k:
        return facts
    subjects = []
    for f in facts:
        s = (f.get("trigger") or "").strip() or "?"
        if s not in subjects:
            subjects.append(s)
    quota = max(2, -(-k // len(subjects)))  # 向上取整，每个主题至少 2 条

    picked: list[dict] = []
    seen: dict[str, int] = {}
    for f in facts:  # 已经按分数排好，先到先得
        s = (f.get("trigger") or "").strip() or "?"
        if seen.get(s, 0) >= quota:
            continue
        seen[s] = seen.get(s, 0) + 1
        picked.append(f)
    # 配额没用完（有的主题不足），按分数补足
    if len(picked) < k:
        rest = [f for f in facts if f not in picked]
        picked.extend(rest[: k - len(picked)])
    picked.sort(key=lambda f: -f["score"])
    return picked[:k]


def _facts_block(facts: list[dict]) -> str:
    """把检索到的事实编成带编号的材料块。编号就是后面引用的下标。"""
    lines = []
    for i, f in enumerate(facts, 1):
        title = (f.get("trigger") or f.get("subject") or "论文").strip()
        lines.append(f"[{i}] {f['action']}（论文：{title[:60]}）")
    return "\n".join(lines)


def _terms_in(question: str, facts: list[dict]) -> list[dict]:
    """问题或事实里出现的术语。术语表攒了 100 个，现成的白话解释。"""
    blob = question + " " + " ".join(
        f"{f.get('trigger','')} {f['action']}" for f in facts
    )
    db = gl.load_db()
    hits = []
    for k, e in db.items():
        if gl._on_page(e["term"], blob):
            hits.append(e)
    # 出现次数多的排前面——那是在多篇论文里反复出现的词，更值当讲
    hits.sort(key=lambda e: -int(e.get("count", 0)))
    return hits[:6]


def answer(question: str, k: int = 12, dry: bool = False) -> dict:
    question = question.strip()
    if not question:
        return {"question": "", "answer": "", "used": [], "facts": [], "terms": [], "gap": ""}

    facts = _retrieve(question, k)
    if not facts:
        return {
            "question": question,
            "answer": "",
            "used": [],
            "facts": [],
            "terms": [],
            "gap": "知识库里没检索到相关的事实。换个问法，或者先让它多学几周。",
        }

    terms = _terms_in(question, facts)
    if dry:
        return {
            "question": question,
            "answer": "",
            "used": [],
            "facts": facts,
            "terms": terms,
            "gap": "",
        }

    data = sv.ollama_json(
        _PROMPT.format(
            facts=_facts_block(facts),
            question=question[:300],
        )
    )
    if not data:
        return {
            "question": question,
            "answer": "",
            "used": [],
            "facts": facts,
            "terms": terms,
            "gap": "模型没返回，下面只列检索到的事实。",
        }

    used_raw = data.get("used") or []
    used = [n for n in used_raw if isinstance(n, int) and 1 <= n <= len(facts)]
    # 模型一个编号都没报，但确实答了——保守起见全列上，让读者自己看哪条撑得起哪句
    if not used:
        used = list(range(1, len(facts) + 1))

    # 行话只从回答真正引用的那几条里找。从全部命中事实里找，会带进
    # 没被用上的噪声主题的术语——问"自我纠正"给你解释"本构模型"。
    cited = [facts[n - 1] for n in used]
    terms = _terms_in(question, cited) if cited else []

    return {
        "question": question,
        "answer": (data.get("answer") or "").strip(),
        "used": used,
        "facts": facts,
        "terms": terms,
        "gap": (data.get("gap") or "").strip(),
    }


def render(result: dict) -> str:
    q = result["question"]
    lines = [f"# 问：{q}\n"]
    lines.append(
        f"> {datetime.now().strftime('%Y-%m-%d %H:%M')}　|　"
        f"命中 {len(result['facts'])} 条事实，答里引了 {len(result['used'])} 条\n"
    )

    if result["answer"]:
        lines.append("## 回答\n")
        lines.append(result["answer"])
        lines.append("")
    if result["gap"]:
        lines.append(f"> **没答上的部分**：{result['gap']}\n")

    if result["used"]:
        lines.append("## 依据（每条都能翻回原文）\n")
        for n in result["used"]:
            f = result["facts"][n - 1]
            title = (f.get("trigger") or f.get("subject") or "论文").strip()
            lines.append(f"**[{n}]** {f['action']}")
            lines.append("")
            if f.get("evidence"):
                lines.append(f"> {f['evidence'][:220]}")
                lines.append("")
            page = f"（p.{f['page']}）" if f.get("page") else ""
            src = f.get("source") or ""
            lines.append(f"主题：{title[:70]}　|　{src} {page}")
            lines.append("")

    if result["terms"]:
        lines.append("## 这里面的行话\n")
        for t in result["terms"]:
            lines.append(f"- **{t['term']}** — {t['plain']}")
        lines.append("")

    if not result["answer"] and not result["used"]:
        lines.append("这次没答上来。要么是库里真没有，要么是问法跟库里的写法差得远。")
        lines.append("")
        if result["facts"]:
            lines.append("检索到但没用上的：")
            for f in result["facts"][:5]:
                lines.append(f"- {f['action'][:90]}")
            lines.append("")

    return "\n".join(lines)


def save(result: dict) -> Path | None:
    if not result.get("question"):
        return None
    QA_DIR.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^\w一-龥]+", "", result["question"])[:24] or "问题"
    path = QA_DIR / f"问答_{label}_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    path.write_text(render(result), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="问知识库：把学过的东西用人话答出来")
    ap.add_argument("question", nargs="*", help="要问的问题（不给就进交互模式）")
    ap.add_argument("--k", type=int, default=10, help="最多检索几条事实（默认 10）")
    ap.add_argument("--dry", action="store_true", help="只看命中什么，不调模型")
    args = ap.parse_args()

    questions = [" ".join(args.question).strip()] if args.question else []

    if not questions:
        print("问知识库学过的东西。直接回车退出。")
        print("例：共识算法的通信复杂度 / 自我纠正为什么有用\n")
        while True:
            try:
                q = input("问> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q:
                break
            questions.append(q)
            _run_one(q, args.k, args.dry)
        return 0

    for q in questions:
        _run_one(q, args.k, args.dry)
    return 0


def _run_one(question: str, k: int, dry: bool) -> None:
    result = answer(question, k=k, dry=dry)
    if dry:
        print(f"\n命中 {len(result['facts'])} 条：")
        for i, f in enumerate(result["facts"], 1):
            print(f"  [{i}] {f['action'][:80]}")
        if result["terms"]:
            print("术语：" + "、".join(t["term"] for t in result["terms"]))
        return

    print()
    if result["answer"]:
        print(result["answer"])
    if result["gap"]:
        print(f"\n（没答上的部分：{result['gap']}）")
    if result["used"]:
        print(f"\n依据 {len(result['used'])} 条：")
        for n in result["used"]:
            f = result["facts"][n - 1]
            src = f.get("source") or ""
            print(f"  [{n}] {f['action'][:60]} — {src}")
    if result["terms"]:
        print("\n行话：" + "、".join(t["term"] for t in result["terms"]))

    path = save(result)
    if path:
        print(f"\n存了：{path}")


if __name__ == "__main__":
    raise SystemExit(main())
