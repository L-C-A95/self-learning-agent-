"""无人值守研究循环 —— 自己提问、自己找论文、自己判断学到没有。

和 `autonomous_loop.py` 的区别：那个只负责"提问"，然后调 auto_research 上网搜。
这个走完整闭环，而且**带判定**——学没学到不由模型自己说了算。

一轮做五件事：

  1. 计划   读「研究方向.txt」+ 已掌握的事实，自己想一个具体问题
  2. 检索   把这个问题的英文关键词丢给 arXiv，按标题筛，不相关的论文不下
  3. 阅读   新论文增量建索引，然后在语料库上提问
  4. 判定   报告必须过质检；过不了就记成"没学到"，**绝不入库**
  5. 沉淀   通过的报告，把带页码的结论写进规则库

最后产出一份人话报告：今晚学到什么、哪些没学到、为什么、下一步建议。

    python research_daemon.py                    默认预算（40 分钟 / 3 轮 / 6 篇）
    python research_daemon.py --minutes 20 --rounds 1 --papers 2
    python research_daemon.py --dry-run          只做计划，不下载不提问

设计上有一条硬规则：**它不能靠"我觉得学到了"来结束一轮。**
每轮必须落一个可核对的结果：几条带页码的结论，或者一条明确的失败原因。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = Path(__file__).resolve().parent
PAPER_DIR = WORK / "文献库"
INDEX_DB = PAPER_DIR / "index.db"
TOPICS_FILE = Path(os.environ.get("DEEPDIVE_TOPICS", str(WORK / "研究方向.txt")))
ASKED_FILE = Path(os.environ.get("DEEPDIVE_RULE_DIR", str(Path(__file__).resolve().parent / "规则库"))) / "asked.json"
JOURNAL = WORK / "日志" / "研究日志.jsonl"
NIGHT_DIR = WORK / "产出" / "夜间记录"
DEEPDIVE = Path(r"D:\anaconda3\Scripts\deepdive.exe")
MODEL = os.environ.get("DEEPDIVE_MODEL", "qwen3:8b")
EMBED = os.environ.get("DEEPDIVE_EMBED", "bge-m3")
OLLAMA = "http://127.0.0.1:11434/api/chat"

sys.path.insert(0, str(WORK))
(WORK / "日志").mkdir(exist_ok=True)
import find_papers  # noqa: E402
import qc_gate  # noqa: E402
import vector_kb  # noqa: E402


# ---------------------------------------------------------------- 工具

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_topics() -> list[str]:
    if not TOPICS_FILE.exists():
        return []
    lines = [ln.strip() for ln in TOPICS_FILE.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def load_asked() -> list[str]:
    if ASKED_FILE.exists():
        try:
            return json.loads(ASKED_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_asked(items: list[str]) -> None:
    ASKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ASKED_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def journal(record: dict) -> None:
    record["时间"] = now()
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


DUPLICATE_AT = 0.5   # 与已问过的问题相似度超过这个值，就算重复

# 小模型「再想一个新问题」往往只会重复上一句（实测三次相似度 1.00）。
# 与其让它自由发挥，不如给一个具体角度 —— 有角度的约束，它才换得动。
ANGLES = [
    "方法对比：两种做法在同一任务上的差别到底在哪",
    "失效条件：什么情况下这个方法会不管用",
    "代价：为了提升效果，付出了什么（数据量、算力、标注成本）",
    "边界：这个结论的适用范围到哪里为止",
    "评测：现在的指标是否真的衡量了想要的东西",
    "迁移：这套做法能不能搬到相邻任务上",
]


def is_duplicate(question: str, known: list[str]) -> tuple[bool, str]:
    """和问过的问题比相似度。

    不能用子串比 —— 拿一个完整疑问句去和主题词比 `q in t`，几乎永不命中，
    这是旧版把同一个问题问两遍的原因。
    """
    q = vector_kb.tokens(question)
    for item in known:
        score = vector_kb.similarity(q, vector_kb.tokens(item))
        if score >= DUPLICATE_AT:
            return True, f"{item[:38]}（相似度 {score:.2f}）"
    return False, ""


def llm_json(prompt: str, timeout: int = 240) -> dict | None:
    """要一段 JSON。qwen3 的思考链已关，这里仍然只会拿 content。"""
    payload = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0.4},
        "messages": [{"role": "user", "content": prompt}],
    }
    import urllib.request

    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = (body.get("message") or {}).get("content", "")
        return json.loads(content)
    except Exception as exc:
        print(f"  · 规划失败：{type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------- 1. 计划

def known_state(topic: str) -> tuple[str, int, list[str]]:
    """只取**和当前方向相关**的已知事实。

    早先版本把全部事实一股脑喂进去，结果它把 AI 对齐里的「自奖励推理」
    套到了「相变储能」上，编出一个不存在的研究题目。跨方向串味的根源
    就在这里：上下文里混进了别的领域的结论。
    """
    facts = vector_kb.all_rules("fact")
    topic_tokens = vector_kb.tokens(topic)
    relevant = []
    for fact in facts:
        blob = vector_kb.tokens(f"{fact['subject']} {fact['action']}")
        if vector_kb.similarity(topic_tokens, blob) >= 0.12:
            relevant.append(fact)
    context = "\n".join(f"- {r['action'][:150]}" for r in relevant[:20]) or "（该方向暂无已知事实）"
    return context, len(relevant), load_asked()


def make_plan(
    topic: str,
    context: str,
    asked: list[str],
    known_count: int,
    others: list[str],
    hint: str = "",
    angle: str = "",
    banned: list[str] | None = None,
) -> dict | None:
    other_line = (
        "禁止混入的其它方向（不要把这些方向的材料、方法搬进本次提问）：\n"
        + "\n".join(f"- {o}" for o in others)
        if others
        else "（当前只有一个方向）"
    )
    angle_line = f"\n本次提问角度（必须按这个角度问）：{angle}\n" if angle else ""
    banned_line = (
        "\n下面这些问题本次**禁止**再问（已经试过了）：\n"
        + "\n".join(f"- {b}" for b in banned)
        + "\n"
        if banned
        else ""
    )
    prompt = f"""你是研究员，正在专门研究这个方向：{topic}

{other_line}
{angle_line}
该方向已知的事实（{known_count} 条，都有出处）：
{context}

已经问过的问题（不要重复或近似）：
{chr(10).join('- ' + q for q in asked[-8:]) or '（暂无）'}
{banned_line}
{f'{chr(10)}上一次的问题被判定跑题或重复：{hint}。请重写。' if hint else ''}

请**只在这个方向内**提出一个尚未解决的具体问题，并为它生成 arXiv 检索关键词。

硬性要求：
1. question_zh：一句中文疑问句，具体（带方法、场景或变量），不要宏观问题。
2. query_en：3~6 个英文关键词，空格分隔，必须**包含方向里的核心词**，
   且不得包含上面「禁止混入的其它方向」里的专有词、材料名或方法名。
3. reason：一句话说明为什么值得查（不超过 40 字）。

只输出 JSON：{{"question_zh": "...", "query_en": "...", "reason": "..."}}"""
    data = llm_json(prompt)
    if not data:
        return None
    question = (data.get("question_zh") or "").strip()
    query = (data.get("query_en") or "").strip()
    if not question or not query:
        return None
    query = re.sub(r"[^A-Za-z0-9\-\s]", " ", query)
    return {
        "question": question[:160],
        "query": " ".join(query.split())[:120],
        "reason": (data.get("reason") or "")[:80],
    }


def on_topic(query: str, topic: str, others: list[str]) -> tuple[bool, str]:
    """检索词必须落在本方向内，否则不下论文。

    arXiv 会老老实实按你给的关键词返回结果，所以串味的关键词一定搜得到
    东西——搜到的却全是别的学科。两道检查卡在下载之前：

      1. 必须命中本方向的核心词
      2. 不能带上「只属于其它方向」的词（把别方向的材料/方法搬过来）
    """
    q = vector_kb.tokens(query)
    t = vector_kb.tokens(topic)
    shared = q & t
    need = 1 if len(t) < 4 else 2
    if len(shared) < need:
        return False, f"没命中本方向核心词（只对上 {sorted(shared) or '无'}）"

    own = t
    foreign = set()
    for other in others:
        foreign |= vector_kb.tokens(other) - own
    leaked = (q & foreign) - t
    if len(leaked) >= 2:
        return False, f"混入了其它方向的词：{sorted(leaked)[:6]}"

    # 第三道：就算没撞上已配置的其它方向，也不能带一堆本方向没有的新词。
    # 「phase change material quantum tunneling qubits」这种混搭必须挡住 ——
    # 它同时满足"含本方向词"和"不撞其它已配置方向"，前两道都拦不住。
    novel = {w for w in q - t if w not in find_papers.GENERIC_HEADS and len(w) > 2}
    # 带连字符的新词基本都是"从别处搬来的方法名"（self-rewarding、transformer-based）。
    # 它不算限定词，只要它的组成部分和本方向毫无关系，就当作串味。
    # 注意：**不要**去限制普通限定词的数量（compressor、temperature、transient
    # 这些是正常的细化词）。我试过按个数卡，结果把正常问题全拦了。
    strange = {
        w
        for w in novel
        if "-" in w and not (set(w.split("-")) & t)
    }
    if strange:
        return False, f"带入了本方向之外的方法名：{sorted(strange)[:4]}"
    return True, ""


# ---------------------------------------------------------------- 2. 检索 + 下载

def library_fallback(topic: str) -> list[dict]:
    """arXiv 没搜到新论文时，退回本地文献库里已有的。

    没有这一步会出现很蠢的情况：库里明明躺着 3 篇完全对题的论文，
    它却说"arXiv 无匹配"然后收工。今晚 22:36 那次就是这么落空的。
    """
    if not PAPER_DIR.exists():
        return []
    out: list[dict] = []
    for pdf in sorted(PAPER_DIR.glob("*.pdf")):
        ok, _ = find_papers.paper_matches(
            {"title": pdf.stem, "summary": ""}, topic, topic=topic, min_anchors=1
        )
        if ok:
            out.append({"title": pdf.stem, "id": pdf.stem.split(" ")[0], "path": str(pdf)})
    return out


def fetch_papers(query: str, max_papers: int, topic: str) -> list[dict]:
    try:
        papers = find_papers.search_arxiv(query, max_papers, topic=topic)
    except Exception as exc:
        print(f"  · 检索失败：{type(exc).__name__}: {exc}")
        papers = []
    if papers:
        kept = []
        for paper in papers:
            got = find_papers.download(paper, PAPER_DIR)
            if got:
                kept.append({"title": paper["title"], "id": paper["id"], "path": str(got)})
        return kept

    fallback = library_fallback(topic)
    if fallback:
        print(f"  · arXiv 没新论文，改用库内已有的 {len(fallback)} 篇")
    return fallback


# ---------------------------------------------------------------- 3. 建索引（增量）

def reindex() -> bool:
    cmd = [
        str(DEEPDIVE), "index", str(PAPER_DIR),
        "-o", str(INDEX_DB),
        "--embed-model", EMBED,
    ]
    code = subprocess.run(
        cmd, cwd=str(WORK), capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).returncode
    if code != 0:
        print(f"  · 建索引失败，返回码 {code}")
    return code == 0


# ---------------------------------------------------------------- 4. 提问 + 判定

def ask_corpus(question: str, minutes_left: float) -> tuple[Path | None, str]:
    out = WORK / f"report_daemon_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    cmd = [
        str(DEEPDIVE), "research", question,
        "--corpus", str(INDEX_DB),
        "-m", MODEL,
        "-o", str(out),
        "--force",
    ]
    timeout = max(60, int(min(minutes_left * 60, 900)))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(WORK),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "提问超时"
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or not out.exists():
        return None, f"提问失败（返回码 {proc.returncode}）"
    return out, log


def sources_on_topic(report_text: str, query: str) -> tuple[bool, str]:
    """报告引用的论文，标题必须和问题对得上。

    两道防线的第二道：第一道在下论文时（标题核心词），这一道在成文后。
    因为库里还有历史论文，提问时可能检索到与本次问题无关的旧文献 ——
    只要它被引用了，就说明这份报告在拿别的学科的结论回答本题。
    """
    block = ""
    match = re.search(r"^##\s+Sources\s*$(.*?)^##\s", report_text, re.M | re.S)
    if match:
        block = match.group(1)
    lines = [ln.strip() for ln in block.splitlines() if re.match(r"^\s*\d+\.\s", ln)]
    if not lines:
        return False, "没有解析到来源列表"
    bad = [ln for ln in lines if not find_papers.title_matches(ln, query)]
    # 一个来源跑题就放行、两个跑题才拦，太松；全都要对题又太严
    # （标题常用缩写）。取 40%：三个来源里错一个还能接受，两个来源错一个就是半数。
    if len(bad) / len(lines) >= 0.4:
        return False, f"{len(bad)}/{len(lines)} 个来源标题与问题不符"
    return True, ""


def evaluate(report: Path, log: str, query: str) -> dict:
    """判定这一轮到底学到没有。这是整个循环里唯一有否决权的地方。"""
    ok, reason = qc_gate.check_report_quality(str(report))
    text = report.read_text(encoding="utf-8", errors="replace")

    # 第二道防线：引用的论文本身得对题
    src_ok, src_why = sources_on_topic(text, query)
    if not src_ok:
        return {
            "ok": False,
            "reason": f"来源与问题不符：{src_why}",
            "claims": 0,
            "grounded": "0/0",
            "dropped_sentences": 0,
            "rejected_claims": 0,
            "facts": [],
            "report": "",
        }
    claims = re.findall(r"^\*\*\d+\.\*\*\s+(.+)$", text, re.M)
    grounded = re.findall(r"_(\d+)/(\d+) claims", text)
    dropped = 0
    for match in re.finditer(r"句子校验删去 (\d+) 句", text):
        dropped = int(match.group(1))
    rejected = len(re.findall(r"claim_support_rejected", log))

    facts = [
        {
            "text": c.strip(),
            "page": None,
        }
        for c in claims
    ]
    # 页码从 Evidence 段里配，一条 claim 后面跟的第一条 [✓] 行
    pages = re.findall(r"\[✓\][^\n]*?p\.(\d+)", text)
    for i, page in enumerate(pages[: len(facts)]):
        facts[i]["page"] = int(page)

    return {
        "ok": bool(ok) and len(facts) >= 3,
        "reason": reason if not ok else f"{len(facts)} 条结论带出处",
        "claims": len(facts),
        "grounded": f"{grounded[0][0]}/{grounded[0][1]}" if grounded else "0/0",
        "dropped_sentences": dropped,
        "rejected_claims": rejected,
        "facts": facts[:6],
        "report": str(report) if ok else "",
    }


# ---------------------------------------------------------------- 主循环

def run(minutes: int, rounds: int, max_papers: int, dry_run: bool) -> None:
    started = time.monotonic()
    deadline = started + minutes * 60

    topics = load_topics()
    if not topics:
        print(f"没有研究方向。请新建 {TOPICS_FILE}，一行一个方向，例如：")
        print("  heat and moisture transfer in building envelopes")
        return

    print(f"=== 无人值守研究 ===")
    print(f"方向：{len(topics)} 个 | 预算：{minutes} 分钟 / {rounds} 轮 / 每轮最多 {max_papers} 篇")
    print(f"模型：{MODEL} | 语料：{INDEX_DB}")

    learned: list[dict] = []
    failed: list[dict] = []
    asked = load_asked()

    for i in range(1, rounds + 1):
        left = (deadline - time.monotonic()) / 60
        if left <= 1:
            print(f"\n预算用完了，提前收工。")
            break

        topic = topics[(i - 1) % len(topics)]
        print(f"\n{'-' * 56}\n第 {i}/{rounds} 轮 · 方向：{topic}（剩余 {left:.0f} 分钟）\n{'-' * 56}")

        context, known_count, _ = known_state(topic)
        others = [t for t in topics if t != topic]
        plan = None
        hint = ""
        banned: list[str] = []
        for attempt in range(3):
            candidate = make_plan(
                topic,
                context,
                asked,
                known_count,
                others,
                hint,
                angle=ANGLES[attempt % len(ANGLES)],
                banned=banned,
            )
            if not candidate:
                continue
            ok, why = on_topic(candidate["query"], topic, others)
            if not ok:
                print(f"  · 第 {attempt + 1} 次规划跑题：{why}，换角度重来")
                hint = f"关键词 {candidate['query']} 跑题了（{why}）"
                banned.append(candidate["question"])
                continue
            # 与已问过的太像，也要重想，而不是直接放弃这一轮。
            # 否则跑过几晚之后，它会每轮 4 秒空转，看起来像坏了。
            dup, who = is_duplicate(candidate["question"], asked + banned)
            if dup:
                print(f"  · 第 {attempt + 1} 次与历史问题重复：{who}，换角度重想")
                hint = f"问题「{candidate['question'][:40]}」和已问过的太像"
                banned.append(candidate["question"])
                continue
            plan = candidate
            break
        if not plan:
            failed.append({"topic": topic, "stage": "计划", "why": "三次都没能想出方向内的新问题"})
            continue

        print(f"问题：{plan['question']}")
        print(f"关键词：{plan['query']}")
        if plan["reason"]:
            print(f"理由：{plan['reason']}")

        asked.append(plan["question"])
        if not dry_run:
            save_asked(asked)

        if dry_run:
            print("  （--dry-run：只做计划，不下载不提问）")
            continue

        print("\n检索 arXiv……")
        papers = fetch_papers(plan["query"], max_papers, topic)
        if not papers:
            print("  · 没有找到标题匹配的论文")
            failed.append({"topic": topic, "stage": "检索", "why": "arXiv 无匹配论文", "query": plan["query"]})
            journal({"类型": "失败", "阶段": "检索", "问题": plan["question"], "关键词": plan["query"]})
            continue
        for p in papers:
            print(f"  · {p['title'][:70]}")

        print("\n更新索引……")
        if not reindex():
            failed.append({"topic": topic, "stage": "索引", "why": "索引失败"})
            continue

        print("\n在语料库里提问……")
        left = (deadline - time.monotonic()) / 60
        report, log = ask_corpus(plan["question"], left)
        if report is None:
            print(f"  · {log}")
            failed.append({"topic": topic, "stage": "提问", "why": log})
            journal({"类型": "失败", "阶段": "提问", "问题": plan["question"], "原因": log})
            continue

        verdict = evaluate(report, log, plan["query"])
        print(
            f"\n判定：{'通过' if verdict['ok'] else '不通过'} · "
            f"{verdict['claims']} 条结论 · grounded {verdict['grounded']} · "
            f"删句 {verdict['dropped_sentences']} · 抽取拦下 {verdict['rejected_claims']} 条"
        )

        if not verdict["ok"]:
            print(f"  · 原因：{verdict['reason']}")
            failed.append(
                {"topic": topic, "stage": "判定", "why": verdict["reason"], "question": plan["question"]}
            )
            journal({"类型": "未通过", "问题": plan["question"], "原因": verdict["reason"]})
            continue

        stats = vector_kb.import_report(report, plan["question"])
        print(f"  · 入库：{stats.get('入库', 0)} 条（跳过 {stats.get('跳过', 0)}）")
        learned.append(
            {
                "topic": topic,
                "question": plan["question"],
                "papers": [p["title"] for p in papers],
                "verdict": verdict,
                "imported": stats.get("入库", 0),
            }
        )
        journal(
            {
                "类型": "学到",
                "问题": plan["question"],
                "论文": [p["title"] for p in papers],
                "结论数": verdict["claims"],
                "入库": stats.get("入库", 0),
            }
        )

    write_summary(learned, failed, topics, time.monotonic() - started, dry_run)

    # 收尾：把今晚学到东西最多的方向，做成一份能读的成果报告。
    # 「日志」告诉你要不要相信它，「成果报告」才是给你的产出。
    # 收尾：无论今晚有没有"学到"，都要给一份成品。
    # 「凭空想一个新缺口」和「库里能答什么」经常对不上，如果只在成功时才出报告，
    # 你会连着几晚什么都拿不到。成果报告走的是另一条路：直接从库里已有的论文
    # 提炼知识点、横向纵向，这条路稳定得多。
    if not dry_run:
        counts: dict[str, int] = {}
        for item in learned:
            counts[item["topic"]] = counts.get(item["topic"], 0) + 1
        if counts:
            best = max(counts, key=counts.get)
        else:
            # 一轮都没成，就挑库里材料最多的那个方向
            scored = [(len(library_fallback(t)), t) for t in topics]
            scored.sort(reverse=True)
            best = scored[0][1] if scored and scored[0][0] else topics[0]
            print(f"\n今晚没有新结论，改为按库内已有材料出报告：{best}")
        print(f"\n为「{best}」生成成果报告……")
        try:
            import survey as survey_mod

            path = survey_mod.build(best, 5, False, None)
            if path:
                print(f"成果报告：{path}")
        except Exception as exc:
            print(f"成果报告生成失败（不影响已入库的结论）：{type(exc).__name__}: {exc}")


def write_summary(
    learned: list[dict], failed: list[dict], topics: list[str], seconds: float, dry_run: bool
) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    NIGHT_DIR.mkdir(parents=True, exist_ok=True)
    path = NIGHT_DIR / f"夜间研究_{stamp}.md"

    lines = [f"# 无人值守研究记录 {now()}", ""]
    lines.append(f"用时 {seconds / 60:.0f} 分钟 · 方向 {len(topics)} 个 · "
                 f"学到 {len(learned)} 轮 · 未成 {len(failed)} 轮")
    if dry_run:
        lines.append("\n> 这是 `--dry-run`，只做了计划，没有下载和提问。")

    lines.append("\n## 学到的（每条都能翻回原文页码）\n")
    if not learned:
        lines.append("没有。这是本次运行的结论，不是失败报告——库里确实没有支撑得住的内容。")
    for item in learned:
        lines.append(f"### {item['question']}\n")
        lines.append(f"来源论文：{'；'.join(t[:70] for t in item['papers'])}")
        lines.append(
            f"核对：{item['verdict']['claims']} 条结论，grounded {item['verdict']['grounded']}，"
            f"删句 {item['verdict']['dropped_sentences']}，入库 {item['imported']} 条\n"
        )
        for fact in item["verdict"]["facts"]:
            page = f"（p.{fact['page']}）" if fact["page"] else ""
            lines.append(f"- {fact['text'][:160]}{page}")
        lines.append("")

    lines.append("\n## 没学到的（以及为什么）\n")
    if not failed:
        lines.append("没有失败。")
    for item in failed:
        lines.append(f"- [{item['stage']}] {item.get('question') or item['topic']} —— {item['why']}")

    lines.append("\n## 建议下一步\n")
    if failed and not learned:
        lines.append(
            "本轮全部落空，多半是**关键词不对**或**该方向在 arXiv 上没有全文**。"
            "先改 `研究方向.txt` 里的措辞，用论文标题里会出现的词，别用日常说法。"
        )
    elif failed:
        lines.append("部分落空。看一眼上面失败原因，如果反复卡在「校验不通过」，说明论文质量或相关度不够。")
    else:
        lines.append("这一轮全部通过。可以加新方向，或者让它连续跑几晚积累。")
    lines.append(f"\n历史记录：`{JOURNAL.name}`（每轮一行，可筛可查）")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{'=' * 56}\n记录已写入：{path}")
    print(f"规则库累计：{dict((k, v) for k, v in _rule_counts().items())}")


def _rule_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for kind in ("fact", "avoid", "refuse"):
        out[kind] = len(vector_kb.all_rules(kind))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="无人值守研究循环")
    ap.add_argument("--minutes", type=int, default=40, help="总预算分钟数（默认 40）")
    ap.add_argument("--rounds", type=int, default=3, help="最多几轮（默认 3）")
    ap.add_argument("--papers", type=int, default=2, help="每轮最多下几篇论文（默认 2）")
    ap.add_argument("--dry-run", action="store_true", help="只做计划，不下载不提问")
    args = ap.parse_args()
    try:
        run(args.minutes, args.rounds, args.papers, args.dry_run)
    except KeyboardInterrupt:
        print("\n手动中断。已完成的部分都已写入日志。")


if __name__ == "__main__":
    main()
