"""网络自主学习 agent —— 上网找论文、直接读、学进知识库。

和之前那套的区别，只有一条：**它不依赖本地文献库。**

之前的流程是：搜到 → 下载到本地 → 建索引 → 检索 → 可能因为"库里没有"而拒答。
这导致它越来越像一个查本地数据库的东西。

现在的流程是：搜到 → 直接从网上抓全文 → 在内存里读 → 抽知识点 → 学进知识库。
本地不存 PDF、不建索引，问什么查什么。

    python web_agent.py "self-correction large language model reasoning"
    python web_agent.py --from-topics --papers 3 --minutes 20

它做的事：
  1. 在线检索 arXiv（官方 API，返回标题/摘要/年份/PDF 直链）
  2. 逐篇从网上抓 PDF，按页解析成文本（页码就地记录，不落盘）
  3. 每篇抽 3~5 条知识点；引文由程序按句子编号从原文取，不让模型抄
  4. 学进知识库（带 arXiv 链接和页码），并去重
  5. 出一份成果报告：知识点 / 横向 / 纵向 / 方向
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = Path(__file__).resolve().parent
TOPICS_FILE = Path(os.environ.get("DEEPDIVE_TOPICS", str(WORK / "研究方向.txt")))
JOURNAL = WORK / "日志" / "网络研究日志.jsonl"
REPORT_DIR = WORK / "产出" / "成果报告"
UA = {"User-Agent": "local-research-agent/1.0 (personal use)"}

sys.path.insert(0, str(WORK))
(WORK / "日志").mkdir(exist_ok=True)
import find_papers as fp  # noqa: E402
import survey as sv  # noqa: E402
import vector_kb as kb  # noqa: E402


# ---------------------------------------------------------------- 上网抓

def fetch_pages(pdf_url: str, timeout: int = 90) -> list[str]:
    """从网上抓 PDF，返回逐页文本。不写磁盘、不建索引。

    分段解析是为了页码：读第几页就是第几页，不用事后猜。
    """
    req = urllib.request.Request(pdf_url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if len(data) < 10_000:
        raise ValueError("下载到的不是有效 PDF")

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("需要 pypdf：pip install pypdf") from exc

    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    for page in reader.pages:
        raw = page.extract_text() or ""
        # PDF 折行会把单词拆成 "b-\norophosphene"，不修的话引文里全是这种碎词
        raw = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", raw)
        pages.append(raw)
    return pages


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")

# 章节标题。带编号的（"3.1 Experimental Setup"）也要认出来。
_HEAD = re.compile(
    r"^\s*(?:[0-9]+(?:\.[0-9]+)*\.?\s*|[IVX]+\.\s*)?"
    r"(abstract|introduction|related work|background|preliminar(?:y|ies)|"
    r"method(?:s|ology)?|approach|model|experiment(?:s|al setup)?|"
    r"results?|evaluation|analysis|discussion|conclusions?|"
    r"limitations|future work|references|bibliography)\s*$",
    re.I,
)

# 数字越小越先读。引言和背景几乎全是"别人做过什么"，价值最低；
# 相关工作直接跳过——它只是文献罗列，读了也抽不出本论文的成果。
_SECTION_RANK = {
    "results": 1, "result": 1, "experiment": 1, "experiments": 1,
    "experimental setup": 1, "evaluation": 1, "analysis": 1,
    "conclusion": 2, "conclusions": 2, "discussion": 2, "limitations": 2,
    "abstract": 3, "front": 3,
    "method": 4, "methods": 4, "methodology": 4, "approach": 4, "model": 4,
    "introduction": 5,
    "background": 6, "preliminary": 6, "preliminaries": 6,
    "related work": 99, "references": 99, "bibliography": 99,
}


def section_blocks(pages: list[str]) -> list[tuple[str, int, str]]:
    """把正文切成 (章节名, 起始页, 文字)。认不出章节的按 front 处理。"""
    blocks: list[tuple[str, int, str]] = []
    name, page_no, buf = "front", 1, []
    for pno, page in enumerate(pages, 1):
        for line in page.split("\n"):
            m = _HEAD.match(line.strip())
            if m:
                if buf:
                    blocks.append((name, page_no, "\n".join(buf)))
                name, page_no, buf = m.group(1).lower(), pno, []
            else:
                if not buf:
                    page_no = pno
                buf.append(line)
    if buf:
        blocks.append((name, page_no, "\n".join(buf)))
    return blocks


def sentence_index(pages: list[str], limit: int = 300) -> list[tuple[int, str]]:
    """挑着读，不是顺着读。

    一篇 30 页的论文如果从头读 120 句，读到的基本是摘要和引言——也就是
    "为什么做"，而不是"做出来什么"。所以按章节排序：结果/实验优先，
    结论其次，然后才是方法和引言，相关工作直接不要。

    返回 (页码, 句子)，按页码顺序，方便你回原文核对。
    """
    ranked: list[tuple[int, int, str]] = []
    for name, page_no, text in section_blocks(pages):
        rank = _SECTION_RANK.get(name, 3)
        if rank >= 99:
            continue
        for block in re.split(r"\n{2,}", text):
            for sent in _SENT.split(" ".join(block.split())):
                s = sent.strip()
                if 25 <= len(s) <= 600:
                    ranked.append((rank, page_no, s))

    ranked.sort(key=lambda x: x[0])          # 稳定的，同优先级保持原顺序
    picked = ranked[:limit]
    picked.sort(key=lambda x: x[1])          # 取完再按页码排，读起来连贯
    return [(page_no, s) for _, page_no, s in picked]


_EXTRACT = """下面是一篇论文的正文，已按句编号。这些句子是从"结果、实验、结论"几节里优先挑出来的。

请只根据这些句子，提炼 {count} 条**具体的知识点**。

要求：
1. point_zh：一句中文，说清**做到了什么、发现了什么、数字是多少**。
   - 好：「硼磷烯的理论比容量达到 1282 mAh/g」
   - 差：「本文提出了一种方法」「该论文研究了某问题」
2. 优先写：具体数值、对比结果、失效条件、反例。
3. 如果一句里只有动机、背景、"我们认为这很重要"，跳过它，不要写成知识点。
4. evidence：支撑这条知识点的**句子编号**（1 个或几个），必须真实存在于下面的列表。
5. 如果这些句子没有实质内容（只有标题、致谢、参考文献），返回空数组。

只输出 JSON：{{"points":[{{"point_zh":"...","evidence":[12]}}]}}

标题：{title}
摘要：{summary}

编号句子：
{numbered}"""


def _ask_points(pairs, meta, count: str, budget: int) -> list[dict]:
    numbered = "\n".join(f"{i + 1}. {s}" for i, (_, s) in enumerate(pairs))
    data = sv.ollama_json(
        _EXTRACT.format(
            count=count,
            title=meta["title"][:200],
            summary=meta.get("summary", "")[:900],
            numbered=numbered[:budget],
        )
    )
    # 模型偶尔不按格式回，而是回 {"error": "..."}（公式多的论文常见）。
    # 这不是内容为空，是这次没问成，值得换个短输入再试。
    if not isinstance(data, dict) or "points" not in data:
        return []

    out: list[dict] = []
    for raw in data.get("points") or []:
        if not isinstance(raw, dict):
            continue
        claim = (raw.get("point_zh") or "").strip()
        if len(claim) < 8:
            continue
        ids = []
        for n in raw.get("evidence") or []:
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(pairs):
                ids.append(n)
        if not ids:
            continue
        page, quote = pairs[ids[0] - 1]
        out.append({"point": claim, "quote": quote, "page": page})
    return out


def read_and_learn(meta: dict, pages: list[str]) -> list[dict]:
    """读一篇论文，抽知识点，引文逐条核验。

    两次尝试：先按"结果/数字优先、5~7 条"要；要不到就缩到前 60 句、
    降到 3~5 条。公式密集的论文第一次经常会失败（模型回 error），
    缩短之后基本能出结果。
    """
    pairs = sentence_index(pages)
    if len(pairs) < 5:
        return []

    got = _ask_points(pairs, meta, "5~7", 16000)
    if got:
        return got
    return _ask_points(pairs[:60], meta, "3~5", 6000)


# ---------------------------------------------------------------- 学进知识库

def remember(topic: str, meta: dict, points: list[dict]) -> int:
    """写进规则库。事实的出处是 arXiv 链接，不是本地文件。"""
    if not points:
        return 0
    url = f"https://arxiv.org/abs/{meta['id']}"
    conn = kb.connect()
    n = 0
    try:
        for p in points:
            row = (
                kb._rid("fact", topic, p["point"]),
                "fact",
                topic,
                topic,
                p["point"],
                p["quote"],
                url,
                p["page"],
                datetime.now().isoformat(timespec="seconds"),
            )
            n += conn.execute(
                "INSERT OR IGNORE INTO rules"
                " (id,kind,subject,trigger,action,evidence,source,page,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                row,
            ).rowcount
        conn.commit()
    finally:
        conn.close()
    return n


# ---------------------------------------------------------------- 一轮

LAST_NOTE = ""  # 上一次检索为什么空："" 真没有 / rate_limited 限流 / error 出错


def fits(topic: str, paper: dict, need: float = 0.6) -> bool:
    """这篇论文是不是真的属于这个话题。

    追踪线专用：词频统计可以用宽口径的语料，但**深读的那几篇必须严**，
    否则线名写着"城市排水"，读回来的却是"全球大气河"。
    """
    anchors = fp.keywords(topic)
    if not anchors:
        return True
    if not fp.title_matches(paper.get("title", ""), topic):
        return False
    text = f"{paper.get('title','')} {paper.get('summary','')}".lower()
    cover = sum(1 for a in anchors if a in text) / len(anchors)
    return len(anchors) < 3 or cover >= need


def search_online(
    topic: str, n: int, newest: bool, strict: bool = False, expand: float = 4.0
) -> list[dict]:
    """面向"查得到"的检索。

    之前那套判据是为"防止混进无关论文"调的，严到会把明明存在的论文滤掉。
    联网 agent 应该先尽量找全，再靠逐句核验保证质量 —— 召回优先。

    `strict=True` 给追踪线用：追一条线要的是"同一片区域里没读过的新论文"，
    放宽到只匹配一个词就会漂到隔壁去（线名写着"磁通涡旋的量子振荡"，
    读回来的却是"量子反常霍尔"）。这种情况下宁可不放宽，空一期也不算污染。
    """
    global LAST_NOTE
    LAST_NOTE = ""

    anchors = fp.keywords(topic)
    if not anchors:
        return []

    if strict:
        queries = [f'all:"{topic}"']
        if len(anchors) >= 2:
            queries.append(" AND ".join(f"all:{w}" for w in anchors[:2]))
        if len(anchors) >= 3:
            queries.append(" AND ".join(f"all:{w}" for w in anchors[:3]))
    else:
        queries = [
            f'all:"{topic}"',
            " AND ".join(f"all:{w}" for w in anchors[:3]),
            " AND ".join(f"all:{w}" for w in anchors[:2]),
            "all:" + anchors[0],
        ]

    last_err = None
    for q in queries:
        try:
            # expand 是给过滤留的余量。扫语料时一口要几十条，再乘 4 就是
            # 一百多条，arXiv 会直接读超时——那种情况给 1.2 就够。
            hits = fp._fetch(q, max(int(n * expand), 8), newest)
        except Exception as exc:
            last_err = exc
            continue
        kept = []
        # 追踪线要的是"同一片区域里的新论文"，所以比日常检索严一档：
        # 摘要得覆盖六成锚点。放宽到一半的时候，"极端降水对城市排水的影响"
        # 会把"全球大气河"也收进来——它确实讲降水，但已经不在同一条线上了。
        need = 0.6 if strict else 0.5
        for h in hits:
            # 追踪线：标题至少要对上两个词，只有正文蹭到不算
            if strict and not fp.title_matches(h.get("title", ""), topic):
                continue
            text = f"{h.get('title','')} {h.get('summary','')}".lower()
            cover = sum(1 for a in anchors if a in text) / len(anchors)
            if len(anchors) < 3 or cover >= need:
                kept.append(h)
        if kept:
            # 标题命中越多的排前面，而不是原样照抄 arXiv 的顺序
            kept.sort(key=lambda h: -sum(1 for a in anchors if a in h["title"].lower()))
            return kept[:n]

    if last_err is not None and "429" in str(last_err):
        LAST_NOTE = "rate_limited"
        print("  · arXiv 限流了（请求太频繁），过几分钟再跑，不是没有论文")
    elif last_err is not None:
        LAST_NOTE = "error"
        print(f"  · 检索出错：{type(last_err).__name__}: {last_err}")
    return []


def research_topic(topic: str, max_papers: int, newest: bool) -> dict:
    print(f"\n{'=' * 58}\n主题：{topic}\n{'=' * 58}")

    print("① 在线检索……")
    found = search_online(topic, max_papers, newest)

    if not found:
        print("   arXiv 上没有对得上的论文（这个主题基本不在 arXiv 覆盖范围内）")
        return {"topic": topic, "papers": [], "points": [], "learned": 0}

    papers: list[dict] = []
    points: list[dict] = []

    for i, item in enumerate(found, 1):
        print(f"\n② [{i}/{len(found)}] {item['title'][:62]}")
        try:
            pages = fetch_pages(item["pdf"])
        except Exception as exc:
            print(f"   抓取失败：{type(exc).__name__}: {exc}")
            continue
        total_chars = sum(len(p) for p in pages)
        print(f"   网上抓到 {len(pages)} 页 / {total_chars} 字")

        got = read_and_learn(item, pages)
        print(f"   知识点 {len(got)} 条")
        if not got:
            continue

        paper_key = item["id"]
        papers.append(
            {
                "id": paper_key,
                "title": item["title"],
                "year": (item.get("published") or "")[:4],
                "signal": sv.practice_signal(" ".join(pages)),
            }
        )
        for g in got:
            points.append({**g, "paper": paper_key})

    for idx, p in enumerate(points, 1):
        p["id"] = f"k{idx}"

    learned = 0
    if points:
        print(f"\n③ 学进知识库……（{len(points)} 条候选）")
        by_paper: dict[str, list[dict]] = {}
        for p in points:
            by_paper.setdefault(p["paper"], []).append(p)
        for paper_key, group in by_paper.items():
            meta = next(p for p in found if p["id"] == paper_key)
            learned += remember(topic, meta, group)
        print(f"   实际新增 {learned} 条（重复的不会重复记）")

    return {"topic": topic, "papers": papers, "points": points, "learned": learned}


def write_report(topic: str, papers: list[dict], points: list[dict]) -> Path:
    groups = sv.build_horizontal(points) if len(points) >= 4 else []
    directions = sv.build_directions(points, topic) if points else []
    text = sv.render(topic, papers, points, groups, directions)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", topic)[:40]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"网络成果_{safe}_{stamp}.md"
    path.write_text(text, encoding="utf-8")
    return path


def load_topics() -> list[str]:
    if not TOPICS_FILE.exists():
        return []
    return [
        ln.strip()
        for ln in TOPICS_FILE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description="上网查论文、读完学进库")
    ap.add_argument("topic", nargs="?", help="主题（英文原文效果最好）")
    ap.add_argument("--from-topics", action="store_true", help="读 研究方向.txt 里的方向，逐个跑")
    ap.add_argument("--papers", type=int, default=3, help="每个主题最多读几篇（默认 3）")
    ap.add_argument("--minutes", type=int, default=20, help="总预算分钟（默认 20）")
    ap.add_argument("--new", action="store_true", help="按提交时间倒序")
    args = ap.parse_args()

    if args.from_topics:
        topics = load_topics()
        if not topics:
            print(f"没有方向。在 {TOPICS_FILE} 里一行写一个。")
            return
    elif args.topic:
        topics = [args.topic]
    else:
        ap.print_help()
        return

    deadline = time.monotonic() + args.minutes * 60
    results = []
    for topic in topics:
        if time.monotonic() > deadline:
            print("\n预算用完，收工。")
            break
        r = research_topic(topic, args.papers, args.new)
        results.append(r)
        with JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "时间": datetime.now().isoformat(timespec="seconds"),
                        "主题": topic,
                        "论文数": len(r["papers"]),
                        "知识点": len(r["points"]),
                        "新增入库": r["learned"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if r["points"]:
            path = write_report(topic, r["papers"], r["points"])
            print(f"\n成果报告：{path}")

    done = [r for r in results if r["points"]]
    print(f"\n{'=' * 58}")
    print(f"本次：{len(done)}/{len(results)} 个主题有收获，累计新增 {(sum(r['learned'] for r in results))} 条")
    print(f"日志：{JOURNAL.name}")


if __name__ == "__main__":
    main()
