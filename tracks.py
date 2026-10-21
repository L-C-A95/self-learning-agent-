"""追踪线：把"每天换一个学科"改成"一条线连着追几周"。

为什么改：要看出一条线在往哪走，得有**时间序列**——同一个词在连续几个
时间点上的频率。每天换学科的话，每个主题永远只有 1~2 个点，画不出曲线，
那所有"趋势判断"都只能是猜。

所以这里存的不是"今天看了什么"，而是一条线的历次快照：
    第 1 周：读了 2 篇，高频词 {surface: 6, logical: 4, ...}
    第 2 周：读了 2 篇，高频词 {surface: 8, decoder: 5, ...}
等有几期之后，"surface 一直在、decoder 新冒出来"才是可验证的变化，
而不是模型拍脑袋说的"这个方向很有前景"。

    python tracks.py            看现在有几条线、各追到第几周
    python tracks.py --close t01   手动收一条线
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = Path(__file__).resolve().parent
STATE = WORK / "追踪线.json"
REPORT_DIR = WORK / "产出" / "追踪周报"

MAX_ACTIVE = 3   # 同时最多追几条。多了每条都浅，等于没追
MAX_WEEKS = 8    # 追满 8 周就收线，写总结，腾位置给下一条

sys.path.insert(0, str(WORK))
import survey as sv  # noqa: E402
import vector_kb as kb  # noqa: E402


# ---------------------------------------------------------------- 状态

def load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"tracks": [], "n": 0}


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def active(state: dict) -> list[dict]:
    return [t for t in state.get("tracks", []) if t.get("status") == "active"]


def pick(state: dict, limit: int = 1, exclude: tuple = ()) -> list[dict]:
    """挑最久没跑的几条。轮着来，别总追同一条。"""
    pool = [t for t in active(state) if t["id"] not in exclude]
    pool.sort(key=lambda t: t.get("last_run", ""))
    return pool[:limit]


# ---------------------------------------------------------------- 词频

_WORD = re.compile(r"[a-z][a-z\-]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_EN_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "using", "based", "can", "our", "its", "their", "which", "have", "has",
    "not", "but", "via", "into", "than", "then", "also", "such", "these",
    "paper", "results", "propose", "proposed", "show", "shows", "new",
    # 摘要里的套话，出现频率高但对判断趋势毫无信息
    "been", "being", "had", "will", "would", "may", "might", "must", "only",
    "other", "same", "some", "very", "well", "more", "most", "much", "many",
    "both", "each", "between", "during", "under", "over", "after", "before",
    "about", "there", "here", "what", "when", "where", "while", "who", "how",
    "they", "them", "our", "you", "his", "her", "one", "two", "three", "all",
    "study", "studies", "work", "works", "approach", "approaches", "method",
    "methods", "system", "systems", "model", "models", "data", "result",
    "different", "high", "low", "large", "small", "first", "second", "however",
    "therefore", "thus", "hence", "among", "within", "without", "thereby",
}


def _stem(word: str) -> str:
    """复数合成单数。不合并的话 insulator / insulators 会被当成两个词，
    趋势表里就会出现"insulator 首次出现"这种假信号。"""
    if len(word) > 5 and word.endswith("s") and not word.endswith(("ss", "us", "is", "as", "os")):
        return word[:-1]
    return word


def _noise(word: str) -> bool:
    if word in _EN_STOP or word in kb._STOP:
        return True
    if word.isdigit():
        return True
    return all(ch in "的了和是在与及对为把被这那" for ch in word)


def term_counts(papers: list[dict], top: int = 40) -> dict[str, int]:
    """这一期论文标题和摘要里的词频。突发词检测的原料就是这个。"""
    counter: Counter = Counter()
    for paper in papers:
        text = f"{paper.get('title', '')} {paper.get('summary', '')}".lower()
        for word in _WORD.findall(text):
            counter[_stem(word)] += 1
        for run in _CJK.findall(text):
            for i in range(len(run) - 1):
                counter[run[i : i + 2]] += 1
    clean = {w: c for w, c in counter.items() if not _noise(w) and c >= 2}
    return dict(Counter(clean).most_common(top))


def shift(track: dict, recent: int = 1) -> list[dict]:
    """最近几期和之前比，哪些词在涨、哪些是新冒出来的。

    没有足够历史就返回空 —— 只有一期的时候，"趋势"是个不成立的说法。
    """
    hist = track.get("history", [])
    if len(hist) < 2:
        return []
    now, before = hist[-recent:], hist[:-recent]
    if not before:
        return []

    words = set()
    for h in now:
        words |= set(h.get("terms", {}))

    out: list[dict] = []
    for word in words:
        cur = sum(h.get("terms", {}).get(word, 0) for h in now)
        avg = sum(h.get("terms", {}).get(word, 0) for h in before) / len(before)
        if cur < 2:
            continue
        if avg < 0.5:
            out.append({"word": word, "now": cur, "before": 0.0, "kind": "new"})
        elif cur >= avg * 1.8 and cur >= 3:
            out.append({"word": word, "now": cur, "before": round(avg, 1), "kind": "up"})
    out.sort(key=lambda x: -x["now"])
    return out[:12]


def _overlap(hist: list[dict]) -> float | None:
    """最近两期语料的重合度。重合高说明检索到的还是同一批论文，
    "词频没变"是理所当然的，不该被误读成"这个方向停滞了"。"""
    if len(hist) < 2:
        return None
    a, b = set(hist[-1].get("corpus") or []), set(hist[-2].get("corpus") or [])
    if not a or not b:
        return None
    return len(a & b) / len(a | b)


def fade(track: dict, recent: int = 1, limit: int = 8) -> list[str]:
    """以前常见、这一期基本不出现的词。掉下去的也是信号。"""
    hist = track.get("history", [])
    if len(hist) < 2:
        return []
    now = hist[-recent:]
    before = hist[:-recent]
    cur = set()
    for h in now:
        cur |= {w for w, c in h.get("terms", {}).items() if c >= 2}

    out: list[tuple[str, float]] = []
    for word in set().union(*[set(h.get("terms", {})) for h in before]):
        if word in cur:
            continue
        avg = sum(h.get("terms", {}).get(word, 0) for h in before) / len(before)
        if avg >= 2.5:
            out.append((word, avg))
    out.sort(key=lambda x: -x[1])
    return [w for w, _ in out[:limit]]


# ---------------------------------------------------------------- 更新一条线

def advance(track: dict, result: dict) -> None:
    """把这一期的结果记进线里。"""
    papers = result.get("papers") or []
    today = datetime.now().strftime("%Y-%m-%d")

    track["week"] = int(track.get("week", 0)) + 1
    track["last_run"] = today

    known = set(track.get("read_ids", []))
    for paper in papers:
        known.add(paper["id"])
    track["read_ids"] = sorted(known)

    track.setdefault("history", []).append(
        {
            "date": today,
            "week": track["week"],
            "papers": len(papers),
            "points": sum(len(p.get("points") or []) for p in papers),
            "titles": [p["title"][:80] for p in papers],
            # 有语料就用语料算词频：一期只读一两篇时，词频等于那一两篇的
            # 词汇表，跨期比出来的"新词"全是噪声。
            "terms": term_counts(result.get("corpus") or papers),
            "corpus": [x.get("id") for x in (result.get("corpus") or [])][:30],
        }
    )

    if track["week"] >= MAX_WEEKS:
        track["status"] = "done"


# 修饰词：留在检索式里只会稀释命中率，把核心词的位置挤掉
_FILLER = {
    "high", "low", "large", "small", "new", "novel", "different", "various",
    "recent", "system", "systems", "method", "methods", "approach", "approaches",
    "effect", "effects", "impact", "impacts", "behavior", "behaviour", "study",
}


def track_query(query: str) -> str:
    """收敛检索词：去掉修饰词，最多留 5 个。

    模型爱堆修饰语。"phase change materials high temperature thermal loss"
    按顺序截 4 个会变成 "phase change materials high"——"high" 一点用没有，
    反而把 temperature 挤掉了。
    """
    import find_papers as fp

    words = [w for w in fp.keywords(query) if w not in _FILLER][:5]
    return " ".join(words) or query


def viable(query: str, need: int = 2) -> int:
    """这个话题在 arXiv 上还够不够追。

    有些话题（比如"城市排水系统"）在 arXiv 上本来就没几篇，立成线只会
    每期空转。建线前先探一下，不够就不立 —— 空一条线比空转八周好。

    返回 -1 表示"撞上限流，判断不了"。这种情况不能当成"追不动"，
    否则每次请求太密都会误杀一条本来能追的线。
    """
    import web_agent as wa

    hits = wa.search_online(query, need + 1, newest=True, strict=True)
    if wa.LAST_NOTE == "rate_limited":
        return -1
    return len(hits)


def create(state: dict, field: str, plan: dict, result: dict) -> dict:
    """把一个自主挑出来的话题立成一条线。"""
    state["n"] = int(state.get("n", 0)) + 1
    track = {
        "id": f"t{state['n']:02d}",
        "name": (plan.get("subtopic") or plan.get("query") or "未命名")[:40],
        "field": field,
        "query": track_query(plan.get("query", "")),
        "why": plan.get("why", ""),
        "created": datetime.now().strftime("%Y-%m-%d"),
        "last_run": "",
        "week": 0,
        "status": "active",
        "read_ids": [],
        "history": [],
    }
    advance(track, result)
    state.setdefault("tracks", []).append(track)
    return track


# ---------------------------------------------------------------- 周报

_PROGRESS = """这条追踪线叫「{name}」，属{field}，已经追了 {week} 周。

前几周的高频词（按出现次数）：{words}
这周读到的论文要点：
{points}

请用 3 句话说明"这条线走到哪了"：
1. 前几周主要在解决什么问题
2. 这周有什么跟前几周不一样的地方
3. 现在卡在哪——下一步大概率会往哪个方向使劲（**只根据上面这些材料推断，
   不要凭空给时间点，不要给"几年内会商业化"这种没有依据的结论**）

要求：说人话，不要罗列论文标题，不要用"具有重要意义""取得了突破"。
只输出 JSON：{{"lines":["...","...","..."]}}"""


def progress(track: dict, result: dict) -> list[str]:
    papers = result.get("papers") or []
    points = "\n".join(
        f"- {p['point']}" for paper in papers for p in (paper.get("points") or [])[:5]
    )
    if not points:
        return []

    words = ", ".join(
        f"{w}({c})"
        for w, c in sorted(track["history"][-1].get("terms", {}).items(), key=lambda x: -x[1])[:12]
    )
    data = sv.ollama_json(
        _PROGRESS.format(
            name=track["name"],
            field=track.get("field", ""),
            week=track["week"],
            words=words or "（第一期，还没有历史）",
            points=points[:3000],
        )
    )
    if not data:
        return []
    out = [(x or "").strip() for x in (data.get("lines") or [])]
    return [x for x in out if len(x) >= 12][:3]


def _safe(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name)[:30]


def write_report(track: dict, result: dict, lines: list[str]) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    out = [f"# 追踪 · {track['name']} · 第 {track['week']} 周", ""]
    out.append(
        f"_从 {track['created']} 开始追，累计 {len(track['read_ids'])} 篇 / "
        f"{sum(h['points'] for h in track['history'])} 条要点_\n"
    )
    if track.get("why"):
        out.append(f"> 当初为什么立这条线：{track['why']}\n")

    if lines:
        out.append("## 这条线走到哪了\n")
        for i, line in enumerate(lines, 1):
            out.append(f"{i}. {line}")
        out.append("")

    papers = result.get("papers") or []
    if papers:
        out.append("## 这周新读的\n")
        for paper in papers:
            out.append(f"### {paper['title']}")
            out.append(f"`{paper.get('year') or '年份未知'} · {paper['url']}`\n")
            for p in paper.get("points") or []:
                out.append(f"- {p['point']}")
            if paper.get("terms"):
                out.append("\n**这篇里的行话**\n")
                for t in paper["terms"]:
                    out.append(f"- **{t['term']}** — {t['plain']}")
            out.append("")

    fresh = shift(track)
    gone = fade(track)
    if fresh or gone:
        out.append("## 跟前几周比，什么变了\n")
        if fresh:
            out.append("**新冒出来 / 在涨**\n")
            for item in fresh:
                tag = "首次出现" if item["kind"] == "new" else f"从 {item['before']} 涨上来"
                out.append(f"- `{item['word']}` — 这周 {item['now']} 次（{tag}）")
            out.append("")
        if gone:
            out.append("**掉下去的**\n")
            out.append(f"- {', '.join('`' + w + '`' for w in gone)}")
            out.append("")
            # 词换了一批，往往说明检索开始漂。说出来，别让人自己猜。
            if len(gone) >= 3:
                out.append(
                    f"> 前几期常出现的词这期基本不提了，这条线的关注点可能在转移。"
                    f"如果不是你想追的方向，`python tracks.py --close {track['id']}` 收掉重开一条。"
                )
                out.append("")
    else:
        out.append("## 跟前几周比，什么变了\n")
        note = "这次词频没有明显变化。"
        hist = track.get("history", [])
        same = _overlap(hist)
        if same is not None and same >= 0.6:
            note += (
                "\n\n> 原因是这两期检索到的基本是同一批论文"
                f"（重合 {same:.0%}），语料没换，词频自然不动。"
                "隔几天再看才有意义——这也是为什么「趋势」必须靠时间序列，而不是多跑几次。"
            )
        elif len(hist) < 2:
            note += "这是第一期，还没有可比的基线。"
        elif same is None:
            note += "（这两期没存语料清单，判断不了是不是同一批论文。）"
        out.append(note)
        out.append("")

    out.append(
        "\n> 说明：词频是从标题和摘要里数出来的，只说明「这个词被提得多不多」，"
        "不代表它更重要。要判断分量还是得看上面那些要点。"
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{track['id']}_{_safe(track['name'])}_第{track['week']}周_{today}.md"
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def close(track: dict, reason: str = "") -> str:
    """收线，写一份总结。"""
    track["status"] = "done"
    track["closed"] = datetime.now().strftime("%Y-%m-%d")
    if reason:
        track["close_reason"] = reason
    return f"已收线：{track['name']}（追了 {track['week']} 周，{len(track['read_ids'])} 篇）"


# ---------------------------------------------------------------- 看状态

def main() -> None:
    ap = argparse.ArgumentParser(description="看 / 管理追踪线")
    ap.add_argument("--close", default=None, help="收掉某条线，传 id 或名字里的一段")
    args = ap.parse_args()

    state = load()
    if args.close:
        for t in state.get("tracks", []):
            if args.close in t["id"] or args.close in t["name"]:
                print(close(t))
                save(state)
                return
        print(f"没找到：{args.close}")
        return

    tracks = state.get("tracks", [])
    if not tracks:
        print("还没有追踪线。跑一次 explore.py 就会立起第一条。")
        return
    for t in tracks:
        mark = "●" if t["status"] == "active" else "○"
        print(
            f"{mark} {t['id']}  {t['name'][:22]:<24} {t.get('field','')[:12]:<14} "
            f"第 {t['week']} 周  {len(t.get('read_ids', []))} 篇  上次 {t.get('last_run') or '-'}"
        )
    print(f"\n活跃 {len(active(state))} 条 / 上限 {MAX_ACTIVE} 条，追满 {MAX_WEEKS} 周自动收线")


if __name__ == "__main__":
    main()
