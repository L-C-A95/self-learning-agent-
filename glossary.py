"""术语翻译官：把论文里的行话翻成人话。

解决的是「每日发现看不懂」这个问题。原文要点都是中文，但全是术语堆出来的：
「全连接共识的通信复杂度为 O(N²)」——每个字都认识，连起来不知道在说什么。

做法：
  1. 让模型从要点里挑出**外行看不懂的词**，逐个给三段：是什么 / 打比方 / 在这篇里指什么
  2. 挑出来的词必须能在原文要点里原样找到，找不到就丢掉（防止模型自己加词）
  3. 解释过一遍的词存进《术语表》，下次遇到直接复用，不再问模型
     —— 同一个词这次和上次说法不一致，比不解释更让人糊涂

    python glossary.py            看看术语表里现在有什么
    python glossary.py --rebuild  按术语表重新写一份 产出/术语表.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = Path(__file__).resolve().parent
TERM_DB = WORK / "知识库" / "术语表.json"
TERM_MD = WORK / "产出" / "术语表.md"

sys.path.insert(0, str(WORK))
import survey as sv  # noqa: E402


# ---------------------------------------------------------------- 提示词

_PROMPT = """你在给一个非专业读者讲解论文。下面是论文《{title}》的要点。

请你挑出其中 4~6 个"外行看不懂的词"，每个用一句人话讲清楚它是什么。

词分两类，**两类都要有**：
A 类（英文缩写、算法名、机制名）：{must}
   —— 这一类是读者最容易卡住的地方，上面点到的每一个都必须讲。
B 类（中文专有名词，比如「拜占庭故障」「通信复杂度」）：自己挑 2 个最关键的。
   不挑「论文」「方法」「系统」「数据」「模型」「节点」这类谁都认识的词。

每个词给两段：
- term：原文里的写法，必须能从下面要点里原样找到（中文写中文，缩写写缩写）
- plain：这东西到底是干什么的，30~55 字。**必须用生活里见过的东西打比方**
  （「就像班里传话，一个人传错，后面全错」）。
  不许用另一个术语来解释它，也不许在解释里再写一遍这个缩写（那是循环）。

对照：
  差 → {{"term":"共识","plain":"多个节点对某值达成一致的协议"}}（术语换术语，等于没说）
  好 → {{"term":"共识","plain":"一群互不信任的机器要就这笔记谁的商量出一个统一答案，就像几个人分头记账，最后必须对得上，否则钱就乱了"}}
  好 → {{"term":"BFT","plain":"系统里有人故意使坏（传假话）时还能对上账，就像打牌时有人说谎，大家靠互相核对仍能算出真实分数"}}

只输出 JSON：{{"terms":[{{"term":"...","plain":"..."}}]}}

论文要点：
{points}"""

_BRIEF = """下面是今天读到的几篇论文各自的要点。

请写一段给人看的导读，让一个完全不懂这个学科的人知道"今天的这几篇在鼓捣什么"。

要求：
1. summary 是 3 句话，每句 30~45 字。
   - 第 1 句：这几篇共同在对付一个什么问题（说人话，别用术语）
   - 第 2 句：它们各自的做法有什么不一样
   - 第 3 句：这件事做成了会有什么用
2. 不要用"本文""该研究""具有重要意义"这种论文腔。就当跟朋友讲。
3. 不要罗列论文标题。
4. 如果这几篇其实没共同点，第 1 句就直说"这几篇关系不大，各干各的"。
5. **如果只有一篇**，就围绕它讲：它在解什么难题 / 用了什么办法 / 做成了有什么用。
   不要说"有的……有的……"，那是在凑数。

只输出 JSON：{{"summary":["...","...","..."]}}

论文要点：
{listing}"""


# ---------------------------------------------------------------- 术语表

def _key(term: str) -> str:
    return re.sub(r"\s+", "", term).lower()


def load_db() -> dict:
    if TERM_DB.exists():
        try:
            return json.loads(TERM_DB.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_db(db: dict) -> None:
    TERM_DB.parent.mkdir(parents=True, exist_ok=True)
    TERM_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 校验

# 解释里出现这些，基本就是在拿术语解释术语，或者在说废话
_EMPTY_TALK = re.compile(r"^(是指|指的是|即|是一种|是一个|用于|用来描述|表示)")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _cjk_len(text: str) -> int:
    return len(_CJK.findall(text or ""))


def _on_page(term: str, blob: str) -> bool:
    """词得真的在要点里出现过。模型爱顺手加词，这一条挡住它。"""
    t = _key(term)
    if len(t) < 2:
        return False
    flat = _key(blob)
    if t in flat:
        return True
    # 「相位变化存储器（PCM）」这种，缩写和全称都算命中
    core = re.sub(r"[（）()]", "", t)
    return len(core) >= 3 and core in flat


# 要点里出现的英文词。中文要点里还能剩下的英文，基本都是缩写或算法名，
# 而这类词恰恰是外行最卡的地方——列出来强制模型逐个讲，不让它漏。
_EN_TERM = re.compile(r"(?<![A-Za-z])[A-Z][A-Za-z0-9\-]{1,11}(?![A-Za-z])")
_SKIP_EN = {"The", "This", "We", "Our", "In", "For", "And", "With", "An", "A"}
_NOISE_EN = re.compile(r"^[A-Z]\d$")  # O(N2) 拆出来的 N2，不是术语


def must_terms(blob: str, limit: int = 4) -> list[str]:
    out, seen = [], set()
    for word in _EN_TERM.findall(blob or ""):
        low = word.lower()
        if low in seen or word in _SKIP_EN or len(word) < 2 or _NOISE_EN.match(word):
            continue
        seen.add(low)
        out.append(word)
    return out[:limit]


def _keep(item: dict, blob: str) -> bool:
    term = (item.get("term") or "").strip()
    plain = (item.get("plain") or "").strip()
    if not term or not _on_page(term, blob):
        return False
    if _cjk_len(plain) < 18:
        return False
    # 「XX 是指……算法」这种纯定义式，读者照样不懂
    if _EMPTY_TALK.match(plain) and _cjk_len(plain) < 30:
        return False
    # 解释里再写一遍缩写，是循环定义
    if _key(term) in _key(plain) and len(term) <= 6 and _cjk_len(plain) < 34:
        return False
    return True


# ---------------------------------------------------------------- 解析

def explain(points: list[dict], title: str = "", field: str = "") -> list[dict]:
    """读一篇论文的要点，把里面的行话挑出来讲明白。

    返回 [{'term','plain','here'}...]。解析不出来就返回空——宁缺，不编。
    """
    if not points:
        return []
    blob = "；".join((p.get("point") or "") for p in points)
    if _cjk_len(blob) < 60:
        return []

    # 要点太多时截断，模型读不完会开始瞎写
    body = "\n".join(f"- {p.get('point', '')}" for p in points[:12])
    forced = must_terms(blob)
    data = sv.ollama_json(
        _PROMPT.format(
            title=(title or "")[:80],
            must="、".join(forced) if forced else "（这篇要点里没有英文词，跳过 A 类）",
            points=body,
        )
    )
    if not data:
        return []

    db = load_db()
    out: list[dict] = []
    seen: set[str] = set()
    for raw in data.get("terms", []) or []:
        if not isinstance(raw, dict) or not _keep(raw, blob):
            continue
        term = raw["term"].strip()
        k = _key(term)
        if k in seen:
            continue
        seen.add(k)

        plain = raw["plain"].strip()
        # 老词用老解释：同一个词每期说法都不一样，读者反而更乱
        old = db.get(k)
        if old and _cjk_len(old.get("plain", "")) >= 18:
            plain = old["plain"]

        out.append({"term": term, "plain": plain})

        entry = db.get(k, {"term": term, "plain": plain, "count": 0})
        entry["plain"] = plain
        entry["field"] = field or entry.get("field", "")
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last"] = datetime.now().strftime("%Y-%m-%d")
        db[k] = entry

        if len(out) >= 8:
            break

    if out:
        save_db(db)
    return out


def briefing(rounds: list[dict]) -> list[str]:
    """全期导读：三句话，说清今天在鼓捣什么。"""
    listing = []
    for r in rounds:
        for paper in r["papers"]:
            pts = "；".join(p["point"][:90] for p in paper["points"][:3])
            listing.append(f"《{paper['title'][:50]}》：{pts}")
    if not listing:
        return []
    data = sv.ollama_json(_BRIEF.format(listing="\n".join(listing[:8])))
    if not data:
        return []
    out = []
    for line in data.get("summary", []) or []:
        line = (line or "").strip()
        if _cjk_len(line) >= 12:
            out.append(line)
    return out[:3]


# ---------------------------------------------------------------- 写成一份能查的表

def write_md() -> Path | None:
    """把攒下的术语整理成一份 md，按学科分组，常用的排前面。"""
    db = load_db()
    if not db:
        return None

    # 顺手清掉以前混进来的噪声（O(N2) 拆出来的 N2 之类），表越干净越好查
    dirty = False
    for k in list(db):
        entry = db[k]
        if _NOISE_EN.match(entry.get("term", "")) or _cjk_len(entry.get("plain", "")) < 15:
            db.pop(k)
            dirty = True
    if dirty:
        save_db(db)
    if not db:
        return None

    groups: dict[str, list[dict]] = {}
    for entry in db.values():
        groups.setdefault(entry.get("field") or "其他", []).append(entry)

    lines = [f"# 术语表\n", f"_攒下 {len(db)} 个词，最后一次更新 {datetime.now():%Y-%m-%d %H:%M}_\n"]

    for field in sorted(groups, key=lambda f: -len(groups[f])):
        rows = sorted(groups[field], key=lambda e: -int(e.get("count", 0)))
        lines.append(f"\n## {field}\n")
        lines.append("| 术语 | 说人话 | 见过 | 最近 |")
        lines.append("|---|---|---|---|")
        for e in rows:
            plain = (e.get("plain") or "").replace("|", "／").replace("\n", " ")
            term = (e.get("term") or "").replace("|", "／")
            lines.append(f"| **{term}** | {plain} | {e.get('count', 1)} 次 | {e.get('last', '')} |")

    TERM_MD.parent.mkdir(parents=True, exist_ok=True)
    TERM_MD.write_text("\n".join(lines), encoding="utf-8")
    return TERM_MD


def main() -> None:
    ap = argparse.ArgumentParser(description="看 / 重建术语表")
    ap.add_argument("--rebuild", action="store_true", help="重新写一份 产出/术语表.md")
    args = ap.parse_args()

    db = load_db()
    if args.rebuild:
        path = write_md()
        print(f"已写出：{path}" if path else "术语表还是空的")
        return
    print(f"术语表：{len(db)} 个词")
    for e in sorted(db.values(), key=lambda x: -int(x.get("count", 0)))[:30]:
        print(f"  {e.get('term',''):<24} {e.get('plain','')[:40]}")


if __name__ == "__main__":
    main()
