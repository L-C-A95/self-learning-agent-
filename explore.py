"""自己逛：不给定题目，它自己挑方向、上网找论文、读完给你一份发现。

和 `web_agent.py` 的区别：
  web_agent   你给主题，它去查
  这个        没人给题目。它自己从学科里挑，挑完自己找论文，自己决定读哪几篇，
              最后写一份「今天看到了什么」给你看。

每次运行：
  0. 先接手在追的线（`追踪线.json`）。同一条线连着追几周，才攒得出时间序列——
     "这个方向在变热"这种话，只有有了几期词频才不是拍脑袋
  1. 剩下的轮次自由逛：从学科清单里挑一个**最近最少碰**的方向（不会连着几天都看 AI）
  2. 让模型在这个方向里提一个具体话题 + 英文检索词
  3. 上网搜、抓全文、逐篇读、抽知识点
  4. 写进知识库（带 arXiv 链接和页码）
  5. 出一份《每日发现》：每篇一句话讲清做了什么，附原文句子和术语白话解释
  6. 自由逛撞上的话题，追踪线没满就立成一条新线（最多 3 条，追满 8 周自动收线）

    python explore.py                      默认逛 3 轮（最多 2 轮追线 + 1 轮自由）
    python explore.py --rounds 5            逛 5 轮
    python explore.py --field energy        只逛某个领域（不碰追踪线）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = Path(__file__).resolve().parent
STATE = WORK / "探索状态.json"
JOURNAL = WORK / "日志" / "探索日志.jsonl"
DIGEST_DIR = WORK / "产出" / "每日发现"

sys.path.insert(0, str(WORK))
(WORK / "日志").mkdir(exist_ok=True)
import explore_fields  # noqa: E402
import glossary as gl  # noqa: E402
import survey as sv  # noqa: E402
import tracks as tk  # noqa: E402
import vector_kb as kb  # noqa: E402
import web_agent as wa  # noqa: E402


# ---------------------------------------------------------------- 状态

def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"fields": {}, "subtopics": [], "runs": 0}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_field(state: dict, only: str | None = None) -> tuple[str, str]:
    """挑一个最近最少碰的方向。轮着来，才不会连着几天都在看同一个学科。"""
    counts = state.get("fields", {})
    if only:
        for key, _hint in explore_fields.FIELDS:
            if only.lower() in key.lower() or only.lower() in _hint.lower():
                return key, _hint
    ranked = sorted(explore_fields.FIELDS, key=lambda kv: counts.get(kv[0], 0))
    return ranked[0]


# ---------------------------------------------------------------- 自己出题

_PROPOSE = """你在做一次自主的文献浏览。当前学科：{field}
该学科常见的英文术语（用来校准方向）：{hint}
{warn}
这个学科里已经看过的具体话题（不要重复）：
{seen}

请提出一个**具体、值得一看**的话题，用来在 arXiv 上找论文。

要求：
1. subtopic：一句中文，说清想看什么现象或问题（不超过 30 字）。要具体，
   不要"XX的进展""XX综述"这种。
2. query_en：3~5 个英文词，必须是论文标题里会出现的说法。
3. why：一句话说明为什么这个话题有意思（不超过 40 字）。

只输出 JSON：{{"subtopic":"...","query_en":"...","why":"..."}}"""


def propose(field: str, hint: str, seen: list[str], warn: str = "") -> dict | None:
    data = sv.ollama_json(
        _PROPOSE.format(
            field=field,
            hint=hint,
            warn=f"\n注意：{warn}。这次必须留在{field}以内。\n" if warn else "",
            seen="\n".join(f"- {s}" for s in seen[-10:]) or "（还没有）",
        )
    )
    if not data:
        return None
    subtopic = (data.get("subtopic") or "").strip()
    query = (data.get("query_en") or "").strip()
    if len(subtopic) < 4 or not query:
        return None
    return {
        "subtopic": subtopic[:60],
        "query": re.sub(r"[^A-Za-z0-9\-\s]", " ", query)[:100].strip(),
        "why": (data.get("why") or "")[:80],
    }


def on_field(query: str, hint: str) -> bool:
    """检索词得落在本学科里。

    之前出过一次：方向写着"经济与博弈"，它却跑去读量子点光催化。
    判据很简单——检索词和该学科的英文术语至少要有一个共同词。
    """
    return bool(kb.tokens(query) & kb.tokens(hint))


# ---------------------------------------------------------------- 一轮

def one_round(
    field: str,
    plan: dict,
    n_papers: int,
    deadline: float | None = None,
    terms: bool = True,
    skip_ids: tuple = (),
    newest: bool = False,
    strict: bool = False,
    survey: int = 0,
) -> dict:
    print(f"\n{'━' * 58}")
    print(f"方向：{field}　|　话题：{plan['subtopic']}")
    print(f"检索：{plan['query']}")
    if plan["why"]:
        print(f"理由：{plan['why']}")
    print("━" * 58)

    # 追踪线每期都要拿到"还没读过的"，所以多要一些再筛。
    # survey>0 时额外抓一批摘要当语料：词频要从几十篇里数才有统计意义。
    # 只数当期深读的一两篇，得到的其实是这两篇的词汇表——跨期比较毫无意义，
    # 每期都显示"12 个新词首次出现"。
    want = max(n_papers + 3 + len(skip_ids), survey or 0)
    found = wa.search_online(
        plan["query"], want, newest=newest, strict=False, expand=1.2 if survey else 4.0
    )
    if not found and wa.LAST_NOTE == "error":
        # 一口要几十条容易读超时；退一步先拿到能深读的那几篇，
        # 语料没有就没有，总比整轮作废好
        print("  · 检索超时，退回小批量重试")
        found = wa.search_online(plan["query"], want, newest=newest, strict=False)
    corpus = found[:survey] if survey else []

    if strict:
        # 语料可以宽，深读必须严：线名写着"城市排水"，就不能读回"全球大气河"
        found = [x for x in found if wa.fits(plan["query"], x)]
    if skip_ids:
        found = [x for x in found if x["id"] not in skip_ids]
        if found:
            print(f"  · 已经读过 {len(skip_ids)} 篇，跳过；剩下 {len(found)} 篇没看")
    if corpus:
        print(f"  · 顺手扫了 {len(corpus)} 篇摘要，用来算这一期的词频")
    if not found:
        print("  · 这个检索词在 arXiv 上没有结果")
        return {
            "field": field, "plan": plan, "papers": [], "learned": 0,
            "more": [], "corpus": corpus,
        }

    read, skipped = found[:n_papers], found[n_papers:]

    papers, learned = [], 0
    for i, item in enumerate(read, 1):
        print(f"\n  [{i}/{len(found)}] {item['title'][:64]}")
        try:
            pages = wa.fetch_pages(item["pdf"])
        except Exception as exc:
            print(f"      抓取失败：{type(exc).__name__}")
            continue
        chars = sum(len(p) for p in pages)
        print(f"      从网上读到 {len(pages)} 页 / {chars} 字")

        points = wa.read_and_learn(item, pages)
        print(f"      知识点 {len(points)} 条")
        if not points:
            continue

        learned += wa.remember(plan["query"], item, points)

        # 术语解析：把要点里的行话翻成人话。解析失败不影响这一篇的其余部分，
        # 但预算是硬的——时间到了就跳过，别为了解释几个词把整轮拖垮。
        got_terms: list[dict] = []
        if terms and (deadline is None or time.monotonic() < deadline):
            try:
                got_terms = gl.explain(points, item.get("title", ""), field)
            except Exception as exc:
                print(f"      术语没解析出来：{type(exc).__name__}")
            if got_terms:
                print(f"      术语 {len(got_terms)} 个：{'、'.join(t['term'] for t in got_terms)}")

        papers.append(
            {
                "id": item["id"],
                "title": item["title"],
                "year": (item.get("published") or "")[:4],
                "url": f"https://arxiv.org/abs/{item['id']}",
                "summary": item.get("summary", ""),
                "points": points,
                "terms": got_terms,
                "signal": sv.practice_signal(" ".join(pages)),
            }
        )

    print(f"\n  学入知识库：{learned} 条")

    links = synthesize(papers)
    if links:
        print(f"  这几篇之间：{len(links)} 条关系")

    return {
        "field": field,
        "plan": plan,
        "papers": papers,
        "learned": learned,
        "more": [x["title"] for x in skipped],
        "links": links,
        "corpus": corpus,
    }


# ---------------------------------------------------------------- 写给你看

_SYNTH = """下面是同一批读到的几篇论文的要点。

请判断它们之间是什么关系。只写有依据的关系，不要写"它们都很有价值"这种空话。
如果看不出关系，就返回空数组——**宁可没有，也不要凑**。

要求：
1. kind 只能是：印证（多篇支持同一结论）、冲突（结论不一致）、互补（各自解决不同环节）、递进（后者补前者的缺口）。
2. papers：涉及的论文编号（P1、P2…），至少两个。
3. statement：一句中文说清关系，不超过 50 字。
   - 好：「P1 证明了 A 有效，P2 说明 A 在 B 条件下失效，边界不同」
   - 差：「P1 研究了 A，P2 研究了 B」——这只是描述各自做了什么，不算关系。

只输出 JSON：{{"links":[{{"kind":"印证","papers":["P1","P2"],"statement":"..."}}]}}

论文要点：
{listing}"""


def synthesize(papers: list[dict]) -> list[dict]:
    """把一轮里的几篇论文放在一起看，而不是各说各的。"""
    if len(papers) < 2:
        return []
    listing = []
    for i, paper in enumerate(papers, 1):
        pts = "；".join(p["point"][:110] for p in paper["points"][:5])
        listing.append(f"P{i} 《{paper['title'][:56]}》：{pts}")

    data = sv.ollama_json(_SYNTH.format(listing="\n".join(listing)))
    if not data:
        return []

    valid = {f"P{i}" for i in range(1, len(papers) + 1)}
    out = []
    for raw in data.get("links", []) or []:
        ids = [x for x in (raw.get("papers") or []) if x in valid]
        statement = (raw.get("statement") or "").strip()
        if len(ids) < 2 or len(statement) < 8:
            continue  # 只有一篇、或没说清关系的，丢掉
        kind = raw.get("kind")
        if kind not in ("印证", "冲突", "互补", "递进"):
            kind = "关联"
        if not _is_real_relation(statement):
            continue  # 复述各自做了什么，不算关系
        out.append({"kind": kind, "papers": ids, "statement": statement[:110]})
    return out


# 关系句里应当出现"对比"的痕迹：转折、程度、条件、数字。
# 只写"P1 研究了 A，P2 研究了 B"的，是复述，不是关系。
_RELATION_HINTS = re.compile(
    r"但|而|相反|相同|一致|不同|都|均|同样|边界|条件|前提|反而|更高|更低|更快|更慢"
    r"|优于|劣于|冲突|矛盾|互补|继承|缺口"
)
_SEP_REPEAT = re.compile(r"P\d[^，。；]{0,12}(研究|提出|讨论|关注|采用|使用)")


def _is_real_relation(statement: str) -> bool:
    if not _RELATION_HINTS.search(statement):
        return False
    # "P1研究…，P2研究…" 这种句式出现两次以上，就是在复述
    if len(_SEP_REPEAT.findall(statement)) >= 2 and len(statement) < 60:
        return False
    return True


def write_digest(rounds: list[dict]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    lines = [f"# 每日发现 · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]

    good = [r for r in rounds if r["papers"]]
    total_papers = sum(len(r["papers"]) for r in rounds)
    total_points = sum(len(p["points"]) for r in rounds for p in r["papers"])
    total_learned = sum(r["learned"] for r in rounds)
    total_terms = sum(len(p.get("terms") or []) for r in rounds for p in r["papers"])

    lines.append(
        f"_逛了 {len(rounds)} 个方向，读到 {total_papers} 篇，"
        f"摘出 {total_points} 条要点，讲明白 {total_terms} 个术语，"
        f"记进知识库 {total_learned} 条（重复的不算）_\n"
    )

    if not good:
        lines.append("这次什么都没读到。可能是检索词太窄，或者方向本身不在 arXiv 覆盖范围内。")
        DIGEST_DIR.mkdir(parents=True, exist_ok=True)
        path = DIGEST_DIR / f"每日发现_{stamp}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    # 开头先给一段人话导读。不想读细节的，看这三句就够了。
    brief = gl.briefing(good)
    if brief:
        lines.append("\n## 今天这几篇在说什么\n")
        for i, line in enumerate(brief, 1):
            lines.append(f"{i}. {line}")

    idx = 1
    round_index: list[tuple[dict, list[int]]] = []
    num_of: dict[int, int] = {}  # id(paper) -> 文中的编号
    for r in good:
        lines.append(f"\n## {r['field']}\n")
        if r.get("track"):
            lines.append(f"> 追踪线 {r['track']} · 第 {r.get('track_week', 1)} 周（同一话题连着追）\n")
        if r["plan"]["why"]:
            lines.append(f"> 为什么挑这个话题：{r['plan']['why']}\n")
        local: list[int] = []
        for paper in r["papers"]:
            lines.append(f"\n### {idx}. {paper['title']}")
            sign = paper["signal"]
            lines.append(f"`{paper['year'] or '年份未知'} · {sign} · {paper['url']}`\n")
            for p in paper["points"]:
                page = f"（p.{p['page']}）" if p.get("page") else ""
                lines.append(f"- {p['point']}{page}")
            # 挑一条原文引文放在下面，方便他判断这条是不是真的
            if paper["points"]:
                lines.append(f"\n  > {paper['points'][0]['quote'][:200]}")
            # 上面的要点全是行话，这里把词捡出来讲明白
            if paper.get("terms"):
                lines.append("\n**这篇里的行话**\n")
                for t in paper["terms"]:
                    lines.append(f"- **{t['term']}** — {t['plain']}")
            local.append(idx)
            num_of[id(paper)] = idx
            idx += 1
        round_index.append((r, local))

    # 跨论文的关系。单个方向的几篇放在一起看，才看得出"这条线走到哪了"。
    any_link = any(r.get("links") for r, _ in round_index)
    lines.append("\n## 这几篇之间\n")
    if not any_link:
        lines.append("这次几篇之间看不出明确关系（互相印证 / 冲突 / 互补 / 递进），就不硬说了。")
    for r, local in round_index:
        for link in r.get("links", []):
            nums = []
            for tag in link["papers"]:
                try:
                    pos = int(tag[1:]) - 1
                except ValueError:
                    continue
                if 0 <= pos < len(local):
                    nums.append(local[pos])
            if len(nums) < 2:
                continue
            refs = "、".join(f"#{n}" for n in nums)
            lines.append(f"- **{link['kind']}**（{refs}）：{link['statement']}")

    # 本期术语速查。同一批词可能在几篇里都出现，合并成一行，标清出自哪几篇。
    # 只有一篇的时候，上面的「这篇里的行话」已经把词讲全了，再来一张一样的表只是重复。
    merged: dict[str, dict] = {}
    for r in good:
        for paper in r["papers"]:
            num = num_of.get(id(paper))
            for t in paper.get("terms") or []:
                k = t["term"].lower().replace(" ", "")
                slot = merged.setdefault(k, {"term": t["term"], "plain": t["plain"], "from": []})
                if num and num not in slot["from"]:
                    slot["from"].append(num)
    if merged and total_papers > 1:
        lines.append("\n## 本期术语速查\n")
        lines.append("| 术语 | 说人话 | 出现在 |")
        lines.append("|---|---|---|")
        for slot in merged.values():
            refs = "、".join(f"#{n}" for n in sorted(slot["from"])) or "—"
            plain = slot["plain"].replace("|", "／")
            lines.append(f"| **{slot['term']}** | {plain} | {refs} |")

    lines.append("\n## 下次可以接着看（搜到了但这次没读）\n")
    any_more = False
    for r in rounds:
        for title in r.get("more", []):
            lines.append(f"- {title[:96]}")
            any_more = True
    if not any_more:
        lines.append("- 这次每个方向搜到的都读完了，没有剩余")
    lines.append(
        "\n> 说明：上面每条要点都能翻回原文那一页。"
        "标「有代码/数据线索」的只表示论文里提到过，不代表我验证过能跑。"
        "术语解释是给你搭桥用的，不是论文原话；要较真还是看要点后面那页原文。"
    )

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"每日发现_{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")

    # 顺手更新跨期累积的术语表，同一个词不用每期重讲一遍
    gl.write_md()
    return path


# ---------------------------------------------------------------- 主

def main() -> None:
    ap = argparse.ArgumentParser(description="自己挑方向、上网读、写每日发现（含追踪线）")
    ap.add_argument("--rounds", type=int, default=3, help="一共逛几轮（默认 3，其中最多 2 轮用来追线）")
    ap.add_argument("--papers", type=int, default=2, help="每个方向最多读几篇（默认 2）")
    ap.add_argument("--minutes", type=int, default=25, help="总预算（默认 25 分钟）")
    ap.add_argument("--field", default=None, help="只逛某个领域，比如 energy / 物理 / 材料")
    ap.add_argument("--no-terms", action="store_true", help="不解析术语（快一半，但看不懂）")
    ap.add_argument("--tracks-only", action="store_true", help="只追已有的线，不自由探索")
    args = ap.parse_args()

    state = load_state()
    tstate = tk.load()
    deadline = time.monotonic() + args.minutes * 60
    rounds: list[dict] = []

    print("=" * 58)
    print("自己逛 —— 没有题目，它自己挑")
    print("=" * 58)

    # 先追已有的线，这是"纵"：同一条线连着追几周，才攒得出时间序列。
    # 最多占两轮，永远留一轮自由探索——不然只会沿着老路走，发现不了新东西。
    cap = args.rounds if args.tracks_only else min(2, max(0, args.rounds - 1))
    go_tracks = [] if args.field else tk.pick(tstate, limit=cap)
    for track in go_tracks:
        if time.monotonic() > deadline:
            print("\n预算用完，收工。")
            break
        print(f"\n〔追踪 {track['id']} · 第 {track['week'] + 1} 周〕{track['name']}")
        result = one_round(
            track["field"],
            {"subtopic": track["name"], "query": track["query"], "why": ""},
            args.papers,
            deadline=deadline,
            terms=not args.no_terms,
            skip_ids=tuple(track.get("read_ids", [])),
            newest=True,
            strict=True,
            survey=30,
        )
        if not result["papers"]:
            # 没读到新论文就别记一期，空点会把时间序列搅乱
            print("  · 这条线这次没找到新论文，先不动它")
            continue
        result["track"] = track["id"]
        result["track_week"] = track["week"] + 1
        rounds.append(result)
        tk.advance(track, result)
        lines = tk.progress(track, result)
        path = tk.write_report(track, result, lines)
        tk.save(tstate)
        print(f"  周报：{path}")

    free = 0 if args.tracks_only else max(0, args.rounds - len([r for r in rounds if r.get("track")]))
    for i in range(1, free + 1):
        if time.monotonic() > deadline:
            print("\n预算用完，收工。")
            break

        field, hint = pick_field(state, args.field)
        known = [s for s in state.get("subtopics", [])]
        plan, warn = None, ""
        for attempt in range(2):
            candidate = propose(field, hint, known, warn)
            if not candidate:
                continue
            if on_field(candidate["query"], hint):
                plan = candidate
                break
            print(f"  · 第 {attempt + 1} 次跑出「{field}」：{candidate['query']}，重想")
            warn = f"检索词「{candidate['query']}」不属于这个学科"
        if not plan:
            print(f"\n[{i}] {field}：两次都没能留在学科内，跳过")
            state["fields"][field] = state["fields"].get(field, 0) + 1
            continue

        result = one_round(
            field, plan, args.papers, deadline=deadline, terms=not args.no_terms
        )
        rounds.append(result)

        state["fields"][field] = state["fields"].get(field, 0) + 1
        state.setdefault("subtopics", []).append(plan["subtopic"])
        state["subtopics"] = state["subtopics"][-80:]
        state["runs"] = state.get("runs", 0) + 1
        save_state(state)

        # 自由探索撞上的话题，线没满就立成一条新线；满了就先只是看过，
        # 等哪条追完 8 周收掉再腾位置。
        if result["papers"] and len(tk.active(tstate)) < tk.MAX_ACTIVE:
            probe = tk.track_query(plan.get("query", ""))
            enough = tk.viable(probe)
            if enough < 0:
                # 限流不等于追不动，先立着，下次再验
                track = tk.create(tstate, field, plan, result)
                tk.save(tstate)
                print(f"  立成追踪线：{track['id']}「{track['name']}」（这次没探成，下次验证）")
            elif enough >= 2:
                track = tk.create(tstate, field, plan, result)
                tk.save(tstate)
                print(f"  立成追踪线：{track['id']}「{track['name']}」")
            else:
                # 立了也只能空转，占着名额还挡别的线
                print(f"  · 「{probe}」严格检索只剩 {enough} 篇，追不到 8 周，先不立线")

        with JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "时间": datetime.now().isoformat(timespec="seconds"),
                        "方向": field,
                        "话题": plan["subtopic"],
                        "论文": [p["title"] for p in result["papers"]],
                        "知识点": sum(len(p["points"]) for p in result["papers"]),
                        "入库": result["learned"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    path = write_digest(rounds)
    print(f"\n{'=' * 58}\n给你看的：{path}")


if __name__ == "__main__":
    main()
