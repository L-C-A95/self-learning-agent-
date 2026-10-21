"""按关键词从 arXiv 自动找论文，下载 PDF 到文献库，并可选立即建索引。

这是"让它在你不看着的时候自己找论文"的第一步。不需要爬网页、不需要梯子，
arXiv 有官方的结构化接口，一次请求就返回标题、编号、日期和 PDF 直链。

    python find_papers.py "multilayer wall thermal resistance" --max 3
    python find_papers.py "heat exchanger optimization" --max 5 --new --index

参数：
    --max N     最多下几篇（默认 3，别一次拉太多）
    --new       按提交时间倒序（追最新），默认按相关度
    --index     下完之后立刻建索引，这样马上就能提问
    --dry-run   只列出会下哪些，不真下
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ARXIV_API = "https://export.arxiv.org/api/query"
# arXiv 的礼貌规则是每 3 秒最多一次请求。跑得快会被 429 拒绝，
# 而 429 在旧代码里被当成"没结果"，看起来就像"这个方向没有论文"。
_MIN_INTERVAL = 3.0
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
NS = {"a": "http://www.w3.org/2005/Atom"}
DEST = Path(__file__).resolve().parent / "文献库"
DEEPDIVE = Path(r"D:\anaconda3\Scripts\deepdive.exe")
INDEX_DB = DEST / "index.db"
EMBED_MODEL = "bge-m3"
UA = {"User-Agent": "deepdive-local/0.1 (personal use)"}


STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "are", "was",
    "were", "has", "have", "how", "what", "does", "can", "its", "use", "used",
    "based", "using", "study", "analysis", "new", "via", "under",
}

# 这些词几乎每篇论文标题都有，不能当"核心名词"。
# 例："heat pump cycle performance optimization" 的核心词应该是 cycle，
# 不是 optimization —— 用它去卡标题会把一整批论文全挡掉。
GENERIC_HEADS = {
    "optimization", "optimisation", "optimizations", "performance", "analysis",
    "study", "design", "method", "methods", "approach", "approaches",
    "application", "applications", "improvement", "improvements", "evaluation",
    "review", "assessment", "technique", "techniques", "perspective",
    "overview", "advances", "progress", "effect", "effects", "impact",
}


def keywords(query: str) -> list[str]:
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9\-]+", query) if len(w) > 2]
    return [w for w in words if w not in STOPWORDS]


def _variants(query: str) -> list[str]:
    """arXiv 的引号短语搜索很严格，逐级放宽，但**不要一放宽就全都要**。"""
    words = keywords(query)
    out = [f'all:"{query}"']
    if len(words) >= 2:
        out.append(" AND ".join(f"all:{w}" for w in words[:2]))
    if len(words) >= 3:
        out.append(" AND ".join(f"all:{w}" for w in words[:3]))
    if words:
        out.append("all:" + words[0])
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


def head_term(query: str) -> str:
    """查询的核心名词 = 最后一个**非通用**实词。

    英语复合名词是"右中心"的：phase change **material**、thermal energy
    **storage**、building **envelope**，核心都在最后。这一条专治同形异义：
    搜 "phase change material" 会返回 "Phase Change **Memory**"（相变存储器），
    因为它俩共享前两个词。光看关键词命中数抓不住，看核心词一抓就中。

    但标题末尾常挂通用词（optimization / performance / analysis），
    拿它当核心词会把整批论文误挡，所以从后往前跳过它们。
    """
    words = keywords(query)
    for word in reversed(words):
        if word not in GENERIC_HEADS:
            return word
    return ""


def title_matches(title: str, query: str) -> bool:
    """标题里至少要对上两个关键词。

    放宽检索词之后一定会混进不相干的东西（搜 "thermal resistance" 会返回
    "electric resistance"）。这条卡在下载之前，比下载完再后悔便宜得多。

    注意：这里**不**检查核心名词 —— 标题常写缩写，太严会误杀好论文。
    核心名词的检查放在 `paper_matches` 里，用「标题 + 摘要」一起判断。
    """
    words = keywords(query)
    if not words:
        return True
    low = title.lower()
    hits = sum(1 for w in words if _word_in(w, low))
    need = 1 if len(words) == 1 else 2
    return hits >= need


def _word_in(word: str, low_title: str) -> bool:
    """词在标题里吗？带连字符的要拆开再比。

    "self-correction" 和标题里的 "self-rewarding correction" 是同一件事，
    但整串比会失败，拆开后两个部分都能对上。
    """
    if word in low_title:
        return True
    parts = [p for p in word.split("-") if len(p) > 2]
    return bool(parts) and all(p in low_title for p in parts)


def _fetch(search_query: str, max_results: int, newest: bool) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "search_query": search_query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate" if newest else "relevance",
            "sortOrder": "descending",
        }
    )
    req = urllib.request.Request(f"{ARXIV_API}?{params}", headers=UA)
    root = None
    for attempt in range(3):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                root = ET.fromstring(resp.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                time.sleep(6 * (attempt + 1))   # 6s、12s 退避
                continue
            raise
    if root is None:
        return []

    out: list[dict] = []
    for entry in root.findall("a:entry", NS):
        raw_id = (entry.findtext("a:id", default="", namespaces=NS) or "").strip()
        title = " ".join((entry.findtext("a:title", default="", namespaces=NS) or "").split())
        summary = " ".join((entry.findtext("a:summary", default="", namespaces=NS) or "").split())
        published = (entry.findtext("a:published", default="", namespaces=NS) or "")[:10]
        pdf = None
        for link in entry.findall("a:link", NS):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf = link.get("href")
                break
        if not raw_id:
            continue
        out.append(
            {
                "id": raw_id.rsplit("/", 1)[-1],
                "title": title,
                "summary": summary,
                "published": published,
                "pdf": pdf or raw_id.replace("/abs/", "/pdf/"),
            }
        )
    return out


def paper_matches(
    paper: dict, query: str, topic: str | None = None, min_anchors: int = 2
) -> tuple[bool, str]:
    """下载前的最后一道：标题 + 摘要一起看。

    arXiv 的摘要里会写全称（"phase change material (PCM)"），所以用它判断
    比只看标题可靠得多。只看标题时，缩写和同义词会把好论文挡在外面；
    只看摘要时，"phase change memory" 也能蹭到 phase change 两个词。

    给了 `topic` 时，改成用**方向**来核验，而不是用模型自己写出来的检索词：
    检索词是模型生成的一次性产物，方向是你亲手写的、稳定的锚点。

    三条同时要求：
      1. 标题命中至少 2 个查询关键词
      2. 核心名词出现在标题或摘要里（防止同形异义：material ≠ memory）
      3. 摘要覆盖方向锚点的一半以上（防止正题方向被放宽后的杂词蹭进来）
    """
    title = paper.get("title", "")
    summary = paper.get("summary", "")
    if not title_matches(title, query):
        return False, "标题关键词不够"

    if topic:
        # 用方向当锚点：模型写出的检索词是一次性的，方向是你亲手写的、稳定的。
        anchors = keywords(topic)
        head = head_term(topic)
        low_title = title.lower()
        if head and not re.search(rf"\b{re.escape(head)}", low_title):
            return False, f"标题里没有方向的核心词 {head}"
        in_title = [a for a in anchors if a in low_title]
        if len(in_title) < min_anchors:
            return False, f"标题只对上方向词 {in_title or '无'}，不够两个"
        if len(anchors) >= 3:
            text = f"{title} {summary}".lower()
            hit = sum(1 for a in anchors if a in text)
            if hit / len(anchors) < 0.5:
                return False, f"标题+摘要只覆盖方向词的 {hit}/{len(anchors)}，不到一半"
        return True, ""

    head = head_term(query)
    if head and not re.search(rf"\b{re.escape(head)}", f"{title} {summary}".lower()):
        return False, f"标题和摘要里都没有核心词 {head}"
    return True, ""


def meta_by_ids(ids: list[str]) -> dict[str, dict]:
    """一次性按 arXiv 编号取标题/摘要/日期。

    用于给"库里已经下载过的论文"补年份和摘要 —— 那些论文是之前下的，
    我们手上只有文件名，没有年份，排不了时间线。
    """
    if not ids:
        return {}
    params = urllib.parse.urlencode({"id_list": ",".join(ids), "max_results": len(ids)})
    req = urllib.request.Request(f"{ARXIV_API}?{params}", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            root = ET.fromstring(resp.read())
    except Exception:
        return {}

    out: dict[str, dict] = {}
    for entry in root.findall("a:entry", NS):
        raw_id = (entry.findtext("a:id", default="", namespaces=NS) or "").strip()
        if not raw_id:
            continue
        short = raw_id.rsplit("/", 1)[-1]
        base = re.sub(r"v\d+$", "", short)
        out[base] = {
            "id": base,
            "title": " ".join((entry.findtext("a:title", default="", namespaces=NS) or "").split()),
            "summary": " ".join(
                (entry.findtext("a:summary", default="", namespaces=NS) or "").split()
            ),
            "published": (entry.findtext("a:published", default="", namespaces=NS) or "")[:10],
        }
    return out


def search_arxiv(
    query: str,
    max_results: int = 3,
    newest: bool = False,
    topic: str | None = None,
    min_anchors: int = 2,
) -> list[dict]:
    """Query arXiv, relaxing the wording until something comes back — then filter.

    Returns only papers whose *title* actually shares keywords with the query.
    """
    best: list[dict] = []
    used = ""
    for q in _variants(query):
        try:
            hits = _fetch(q, max_results * 3, newest)
        except Exception:
            continue
        kept = [p for p in hits if paper_matches(p, query, topic, min_anchors)[0]]
        if len(kept) > len(best):
            best, used = kept[:max_results], q
        if len(best) >= max_results:
            break

    if best and used != f'all:"{query}"':
        print(f"（原句无结果，已自动放宽为：{used}，并按标题+摘要筛过）\n")
    return best


def safe_name(text: str, limit: int = 70) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].strip()


def download(paper: dict, dest: Path) -> Path | None:
    target = dest / f"{paper['id']} {safe_name(paper['title'])}.pdf"
    if target.exists() and target.stat().st_size > 10_000:
        print(f"  · 已存在，跳过：{target.name}")
        return target
    req = urllib.request.Request(paper["pdf"], headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
    except Exception as exc:
        # arXiv 偶尔给出带版本号的 PDF 直链却 404，退回不带版本号的地址再试一次
        base_id = re.sub(r"v\d+$", "", paper["id"])
        retry = f"https://arxiv.org/pdf/{base_id}"
        if retry != paper["pdf"]:
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(retry, headers=UA), timeout=90
                ) as resp:
                    data = resp.read()
            except Exception:
                print(f"  · 下载失败 {paper['id']}：{type(exc).__name__}: {exc}")
                return None
        else:
            print(f"  · 下载失败 {paper['id']}：{type(exc).__name__}: {exc}")
            return None
    if len(data) < 10_000:
        print(f"  · 文件太小，可能不是 PDF，跳过 {paper['id']}")
        return None
    target.write_bytes(data)
    print(f"  · 已下载 {target.name}（{len(data) / 1024:.0f} KB）")
    return target


def build_index() -> None:
    cmd = [
        str(DEEPDIVE), "index", str(DEST),
        "-o", str(INDEX_DB),
        "--embed-model", EMBED_MODEL,
    ]
    print("\n建索引中（第一次会慢，后面只处理新增文件）……")
    code = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent)).returncode
    if code != 0:
        print(f"建索引失败，返回码 {code}")
        return
    print(f"\n索引已更新：{INDEX_DB}")
    print("现在可以双击 start.bat → 选 2 提问。")


def main() -> None:
    ap = argparse.ArgumentParser(description="从 arXiv 自动找论文并入库")
    ap.add_argument("query", help="关键词，英文效果最好，比如 thermal resistance")
    ap.add_argument("--max", type=int, default=3, help="最多下几篇（默认 3）")
    ap.add_argument("--new", action="store_true", help="按提交时间倒序，追最新")
    ap.add_argument("--index", action="store_true", help="下完立即建索引")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不下载")
    args = ap.parse_args()

    DEST.mkdir(parents=True, exist_ok=True)
    print(f"在 arXiv 搜：{args.query}（{'最新' if args.new else '相关度'}，最多 {args.max} 篇）\n")
    try:
        papers = search_arxiv(args.query, args.max, args.new)
    except Exception as exc:
        print(f"检索失败：{type(exc).__name__}: {exc}")
        print("检查一下网络能不能访问 export.arxiv.org")
        return

    if not papers:
        print("没搜到。换个更通用的英文关键词，比如把 'DTL 热阻' 换成 'thermal resistance'。")
        return

    for i, p in enumerate(papers, 1):
        print(f"[{i}] {p['title'][:78]}")
        print(f"    {p['id']}  {p['published']}")
    print()

    if args.dry_run:
        print("（--dry-run：以上只是预览，什么都没下）")
        return

    saved = []
    for p in papers:
        got = download(p, DEST)
        if got:
            saved.append(got)
    print(f"\n本次新增/确认 {len(saved)} 篇，文献库共 {len(list(DEST.glob('*.pdf')))} 个 PDF。")

    if args.index and saved:
        build_index()
    elif not args.index:
        print("记得跑一次：python find_papers.py --index ... 或者用 start.bat 选 4 建索引。")


if __name__ == "__main__":
    main()
