"""成果报告：一个主题 → 一批论文 → 一份能读的东西。

和 `research_daemon.py` 的分工：
  daemon  每晚自己找题、自己判断学到没有，产出的是"今晚学了什么"
  这个    你给一个主题，它扫一批论文，产出的是"这个方向现在什么样、能往哪走"

报告四段，对应你要的三件事：

  一、知识点总结   每篇论文讲了什么（中文一句话 + 原文英文原句 + 页码）
  二、横向对比     同类工作并排放，差在哪
  三、纵向演进     按年份排，这条路是怎么一步步走到今天的
  四、值得往哪走   每个方向都必须挂到具体知识点上，并标明是"论文说的"还是"我的推断"

设计上有一条硬规则：**模型只能引用已核验的知识点编号，不能凭空写判断。**
不挂依据的方向，渲染时直接丢掉。

    python survey.py "self-correction large language model reasoning" --papers 6
    python survey.py "thermal energy storage building" --papers 8 --new
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = Path(__file__).resolve().parent
REPORT_DIR = WORK / "产出" / "成果报告"
PAPER_DIR = WORK / "文献库"
INDEX_DB = PAPER_DIR / "index.db"
DEEPDIVE = Path(r"D:\anaconda3\Scripts\deepdive.exe")
MODEL = "qwen3:8b"
EMBED = "bge-m3"
OLLAMA = "http://127.0.0.1:11434/api/chat"

sys.path.insert(0, str(WORK))
import find_papers as fp  # noqa: E402

# 可实践性的线索词。这不是判断"能不能用"，只是找出"有没有可复现的痕迹"。
PRACTICE_HINTS = {
    "有代码/数据线索": r"\b(github|code is available|open[- ]source|dataset|benchmark|our implementation)\b",
    "有实验或仿真": r"\b(experiment|experimental|simulation|measured|measurement|testbed)\b",
    "偏理论/综述": r"\b(theoretical (analysis|framework)|survey|review|we prove|theorem)\b",
}


# ---------------------------------------------------------------- LLM

def ollama_json(prompt: str, timeout: int = 420, temperature: float = 0.3) -> dict | None:
    payload = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": temperature},
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return json.loads((body.get("message") or {}).get("content", "") or "{}")
    except Exception as exc:
        print(f"    · 模型调用失败：{type(exc).__name__}: {exc}")
        return None


def _norm(text: str) -> str:
    """比对引文用的归一化：去掉换行连字符、所有空白，转小写。"""
    text = re.sub(r"-\s*\n", "", text or "")
    return re.sub(r"\s+", "", text).lower()


# ---------------------------------------------------------------- 取论文正文

def paper_chunks(pdf_name: str, limit_chars: int = 11000) -> list[tuple[str, int | None]]:
    """从语料索引里取某篇论文的正文块，返回 [(文本, 页码)]。"""
    if not INDEX_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT c.text, p.page_no "
            "FROM chunks c "
            "JOIN documents d ON d.doc_id = c.doc_id "
            "LEFT JOIN pages p ON p.doc_id = c.doc_id "
            "  AND c.offset_start >= p.start_offset AND c.offset_start < p.end_offset "
            "WHERE d.source_path LIKE ? "
            "ORDER BY c.offset_start",
            (f"%{pdf_name}%",),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    out: list[tuple[str, int | None]] = []
    total = 0
    for text, page in rows:
        # 跳过参考文献段，那里没有可用的知识点
        if re.match(r"^\s*(references|bibliography)\b", text, re.I):
            break
        out.append((text, page))
        total += len(text)
        if total >= limit_chars:
            break
    return out


def practice_signal(text: str) -> str:
    hits = [name for name, pat in PRACTICE_HINTS.items() if re.search(pat, text, re.I)]
    return "、".join(hits) if hits else "未见代码/实验线索"


# ---------------------------------------------------------------- 抽知识点

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")


def split_sentences(text: str, limit: int = 140) -> list[str]:
    """把正文切成句子并编号。

    不要求模型"抄一句原文"，而是要求它**报句子编号** —— 抄写会出错
    （实测它会顺手把英文翻成中文），报编号则不可能编造，引文由程序取。
    """
    out: list[str] = []
    for block in re.split(r"\n{2,}", text):
        for sent in _SENT_SPLIT.split(" ".join(block.split())):
            s = sent.strip()
            if 25 <= len(s) <= 600:
                out.append(s)
            if len(out) >= limit:
                return out
    return out


_EXTRACT_PROMPT = """下面是一篇论文的正文，已经按句编号。

请只根据这些句子，提炼 3~5 条**具体的知识点**。

要求：
1. point_zh：一句中文，说清"这篇论文做了什么/发现了什么"。要具体，
   不要"本文提出了一种方法"这种空话。
2. evidence：给出支撑这条知识点的**句子编号**（1 个或几个），必须是上面列表里真实存在的编号。
3. 如果这些句子没有实质内容（只有标题、致谢、参考文献），返回空数组。

只输出 JSON：{{"points":[{{"point_zh":"...","evidence":[12,13]}}]}}

标题：{title}
摘要：{summary}

编号句子：
{numbered}"""


def extract_points(paper: dict, chunks: list[tuple[str, int | None]]) -> list[dict]:
    text = "\n\n".join(t for t, _ in chunks)
    if len(text) < 300:
        return []

    sentences = split_sentences(text)
    if len(sentences) < 3:
        return []
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sentences))

    data = ollama_json(
        _EXTRACT_PROMPT.format(
            title=paper["title"][:200],
            summary=paper.get("summary", "")[:1000],
            numbered=numbered[:9000],
        )
    )
    if not data:
        return []

    points = []
    for raw in data.get("points", []) or []:
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
            if 1 <= n <= len(sentences):
                ids.append(n)
        if not ids:
            print(f"    · 没有有效句子编号，丢弃：{claim[:30]}…")
            continue

        quote = " ".join(sentences[n - 1] for n in ids[:2])
        target = _norm(quote)
        page = None
        for chunk_text, chunk_page in chunks:
            if target[:60] and target[:60] in _norm(chunk_text):
                page = chunk_page
                break

        points.append({"point": claim, "quote": quote, "page": page, "sentences": ids})
    return points


# ---------------------------------------------------------------- 横向与方向

_HORIZONTAL_PROMPT = """下面是同一研究方向上的若干知识点，每条都有编号。

请把它们按"解决的是同一个问题"分组，并在组内比较它们的差异。

要求：
1. 每组至少 2 条知识点，否则不要成组（单条不构成对比）。
2. name：这一组在解决什么问题（不超过 16 字）。
3. point_ids：属于这一组的知识点编号。
4. what_differs：组内做法的主要差异（不超过 60 字，说具体差异，不要评价好坏）。

只输出 JSON：{{"groups":[{{"name":"...","point_ids":["k1","k4"],"what_differs":"..."}}]}}

知识点：
{points}"""


def build_horizontal(points: list[dict]) -> list[dict]:
    listing = "\n".join(f"{p['id']}: {p['point']}" for p in points)
    data = ollama_json(_HORIZONTAL_PROMPT.format(points=listing))
    if not data:
        return []

    valid = {p["id"] for p in points}
    groups = []
    for raw in data.get("groups", []) or []:
        ids = [i for i in (raw.get("point_ids") or []) if i in valid]
        if len(ids) < 2:
            continue
        groups.append(
            {
                "name": (raw.get("name") or "").strip()[:24],
                "ids": ids,
                "differs": (raw.get("what_differs") or "").strip()[:120],
            }
        )
    return groups


_DIRECTION_PROMPT = """下面是某研究方向上已经核验过的知识点，每条有编号。

请提出 3~5 个**值得往哪走**的方向。

硬性要求：
1. 每个方向必须挂到至少 1 条知识点上（basis 里放编号）。**没有依据的方向不要写。**
2. kind 只能填 "论文结论" 或 "推断"：
   - 论文结论：这条知识点本身就明确说了
   - 推断：这是你基于这些知识点外推的，论文没直接说
3. how_to_check：怎么验证这个方向值不值得做（一句话，要可操作）。
4. 不要写"进一步研究""加强探索"这种空话。

只输出 JSON：{{"directions":[{{"direction":"...","basis":["k2"],"kind":"推断","how_to_check":"..."}}]}}

知识点：
{points}"""


def build_directions(points: list[dict], topic: str) -> list[dict]:
    listing = "\n".join(f"{p['id']}: {p['point']}" for p in points)
    data = ollama_json(_DIRECTION_PROMPT.format(points=listing))
    if not data:
        return []

    valid = {p["id"] for p in points}
    out = []
    for raw in data.get("directions", []) or []:
        basis = [i for i in (raw.get("basis") or []) if i in valid]
        text = (raw.get("direction") or "").strip()
        if not basis or len(text) < 8:
            continue  # 没有依据的判断，一律不要
        kind = raw.get("kind") if raw.get("kind") in ("论文结论", "推断") else "推断"
        out.append(
            {
                "direction": text[:160],
                "basis": basis,
                "kind": kind,
                "check": (raw.get("how_to_check") or "").strip()[:120],
            }
        )
    return out


# ---------------------------------------------------------------- 渲染

def short_title(title: str, limit: int = 26) -> str:
    """截断时不要切在单词中间。"""
    title = (title or "").strip()
    if len(title) <= limit:
        return title
    cut = title[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


def render(topic: str, papers: list[dict], points: list[dict], groups: list[dict],
           directions: list[dict]) -> str:
    lines: list[str] = []
    by_id = {p["id"]: p for p in points}
    years = [p["year"] for p in papers if p.get("year")]
    span = f"{min(years)}–{max(years)}" if years else "年份未知"

    lines.append(f"# 成果报告：{topic}\n")
    lines.append(
        f"_生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"{len(papers)} 篇论文（{span}）· {len(points)} 条可核验知识点_\n"
    )
    lines.append(
        "> 判断和事实分开写：标 **论文结论** 的是论文原文说的，"
        "标 **推断** 的是基于这些结论外推的，我自己也不确定。\n"
    )

    # 一、知识点
    lines.append("\n## 一、知识点总结（按论文）\n")
    if not points:
        lines.append("这次没有抽出可核验的知识点。常见原因：论文是扫描件、或正文没被索引。")
    for paper in papers:
        own = [p for p in points if p["paper"] == paper["id"]]
        if not own:
            continue
        lines.append(f"\n### {paper['title'][:110]}")
        meta = f"{paper.get('year') or '年份未知'} · arXiv:{paper['id']} · {paper['signal']}"
        lines.append(f"`{meta}`\n")
        for p in own:
            page = f" · p.{p['page']}" if p["page"] else ""
            lines.append(f"- **{p['point']}**{page}")
            lines.append(f"  > {p['quote'][:220]}")

    # 二、横向
    lines.append("\n## 二、横向对比（同一问题的不同做法）\n")
    if not groups:
        lines.append("这次没能分组——知识点太少或太分散，凑不出可比的组。不硬凑。")
    for group in groups:
        lines.append(f"\n**{group['name']}**")
        titles = {by_id[i]["paper"] for i in group["ids"] if i in by_id}
        name_map = {p["id"]: short_title(p["title"], 30) for p in papers}
        lines.append("涉及：" + "、".join(name_map.get(t, t) for t in sorted(titles)))
        lines.append(f"- 差异：{group['differs']}")
        for i in group["ids"]:
            if i in by_id:
                lines.append(f"  - [{i}] {by_id[i]['point']}")

    # 三、纵向
    lines.append("\n## 三、纵向演进（按年份）\n")
    if not years:
        lines.append("论文年份缺失，排不了时间线。")
    else:
        for paper in sorted(papers, key=lambda x: (x.get("year") or "0000")):
            own = [p for p in points if p["paper"] == paper["id"]]
            if not own:
                continue
            head = own[0]["point"]
            lines.append(
                f"- **{paper.get('year') or '?'}** · {paper['title'][:78]}\n"
                f"  → {head}"
            )
        lines.append(
            "\n> 注意：这里只按 arXiv 提交年份排序，**不代表这个方向的最早工作**。"
            "更早的经典论文大多在期刊上，arXiv 没有。"
        )

    # 四、方向
    lines.append("\n## 四、值得往哪走\n")
    if not directions:
        lines.append("没有产出方向建议——可能是因为知识点不足以支撑外推。宁缺勿滥。")
    for d in directions:
        lines.append(f"\n**{d['direction']}**")
        lines.append(f"- 性质：{d['kind']}")
        lines.append(f"- 依据：{'、'.join(d['basis'])}")
        for i in d["basis"]:
            if i in by_id:
                lines.append(f"  - [{i}] {by_id[i]['point']}")
        if d["check"]:
            lines.append(f"- 怎么验证：{d['check']}")

    # 五、局限
    lines.append("\n## 五、这份报告没做到的\n")
    lines.append(
        "- **只覆盖 arXiv。** 期刊论文（Elsevier / ASHRAE / 知网）不在里面，"
        "所以「这个方向只有这些工作」不成立，只能说「arXiv 上有这些」。"
    )
    lines.append("- **没有引用数。** 影响力需要 Semantic Scholar 才能看，这次没接。")
    lines.append(
        "- **可实践性只是线索，不是结论。** 标「有代码/数据线索」只说明论文里提到过，"
        "不代表真能跑起来。"
    )
    empty = [p["title"][:60] for p in papers if not any(x["paper"] == p["id"] for x in points)]
    if empty:
        lines.append(f"- **{len(empty)} 篇没抽出可用知识点**：{'；'.join(empty)}")

    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def reindex() -> None:
    subprocess.run(
        [str(DEEPDIVE), "index", str(PAPER_DIR), "-o", str(INDEX_DB), "--embed-model", EMBED],
        cwd=str(WORK),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def library_papers(topic: str) -> list[dict]:
    """本地文献库里已经存在的、和这个主题对得上的论文。

    只靠 arXiv 现搜会漏掉之前已经下过的好论文；成果报告应该把它们都用上。
    文件名形如 "2310.01798v2 Large Language Models Cannot Self-Correct..."，
    前半段是 arXiv 编号，用它回去补年份和摘要。
    """
    if not PAPER_DIR.exists():
        return []

    picked: list[dict] = []
    ids: list[str] = []
    for pdf in sorted(PAPER_DIR.glob("*.pdf")):
        stem = pdf.stem
        matched, _ = fp.paper_matches(
            {"title": stem, "summary": ""}, topic, topic=topic, min_anchors=1
        )
        if not matched:
            continue
        m = re.match(r"(\d{4}\.\d{4,5})v\d+", stem)
        arxiv_id = m.group(1) if m else stem.split(" ")[0]
        picked.append({"id": arxiv_id, "title": stem[m.end():].strip() if m else stem,
                       "summary": "", "year": "", "pdf": pdf.name})
        if m:
            ids.append(arxiv_id)

    if ids:
        meta = fp.meta_by_ids(ids[:20])
        for item in picked:
            info = meta.get(re.sub(r"v\d+$", "", item["id"]))
            if info:
                item["title"] = info["title"] or item["title"]
                item["summary"] = info["summary"]
                item["year"] = info["published"][:4]
    return picked


def build(topic: str, max_papers: int, newest: bool, out: Path | None) -> Path | None:
    print(f"主题：{topic}")
    print(f"检索 arXiv（{'最新' if newest else '相关度'}，最多 {max_papers} 篇）……")
    try:
        # 成果报告要横向比，需要多几篇，所以方向锚点放宽到 1 个；
        # 每篇的知识点仍然逐句核验，不会因为放宽就编。
        found = fp.search_arxiv(topic, max_papers, newest, topic=topic, min_anchors=1)
    except Exception as exc:
        print(f"检索失败：{type(exc).__name__}: {exc}")
        return None
    if not found:
        print("\narXiv 上没找到对得上的新论文（库里已有的仍会用）。")

    papers: list[dict] = []
    seen: set[str] = set()

    # 1) 库里已有的（速度快、和主题对得上）
    for item in library_papers(topic):
        base = re.sub(r"v\d+$", "", item["id"])
        if base in seen or not Path(PAPER_DIR, item["pdf"]).exists():
            continue
        seen.add(base)
        papers.append(item)

    # 2) 再去 arXiv 补新的
    for item in found:
        base = re.sub(r"v\d+$", "", item["id"])
        if base in seen:
            continue
        path = fp.download(item, PAPER_DIR)
        if not path:
            continue
        seen.add(base)
        papers.append(
            {
                "id": item["id"],
                "title": item["title"],
                "summary": item.get("summary", ""),
                "year": (item.get("published") or "")[:4],
                "pdf": path.name,
            }
        )
    print(f"\n纳入分析：{len(papers)} 篇（含库内已有）\n")
    papers = papers[:max_papers]  # 每篇要跑一次抽取，太多会拖很久

    if not papers:
        return None

    print("更新索引……")
    reindex()

    points: list[dict] = []
    for i, paper in enumerate(papers, 1):
        chunks = paper_chunks(paper["pdf"])
        paper["signal"] = practice_signal(" ".join(t for t, _ in chunks))
        print(f"[{i}/{len(papers)}] {paper['title'][:58]}")
        got = extract_points(paper, chunks)
        for item in got:
            points.append({**item, "paper": paper["id"]})
        print(f"    · 知识点 {len(got)} 条")

    for idx, p in enumerate(points, 1):
        p["id"] = f"k{idx}"
    print(f"\n可核验知识点合计：{len(points)} 条")

    groups = build_horizontal(points) if len(points) >= 4 else []
    print(f"横向分组：{len(groups)} 组")
    directions = build_directions(points, topic) if points else []
    print(f"方向建议：{len(directions)} 条")

    text = render(topic, papers, points, groups, directions)

    if out is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", topic)[:40]
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / f"成果报告_{safe}_{stamp}.md"
    out.write_text(text, encoding="utf-8")
    print(f"\n已写入：{out}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="按主题产出一份成果报告")
    ap.add_argument("topic", help="主题（英文原文效果最好）")
    ap.add_argument("--papers", type=int, default=6, help="最多看几篇（默认 6）")
    ap.add_argument("--new", action="store_true", help="按提交时间倒序")
    ap.add_argument("--out", default=None, help="输出路径")
    args = ap.parse_args()

    out = Path(args.out) if args.out else None
    result = build(args.topic, args.papers, args.new, out)
    if not result:
        sys.exit(1)


if __name__ == "__main__":
    main()
