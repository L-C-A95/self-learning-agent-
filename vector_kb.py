"""规则库 —— 存"下次该怎么做不同"，不存报告正文。

旧版存什么：把报告按段落切开，整段塞进向量库。问题是报告里 60% 的字是
Evidence / Sources / 表格 / 元信息，那些是排版产物，不是知识。结果就是
"库里有 143 条"，检索出来全是噪声。

新版存三类东西，每条都必须可追溯、可撤销：

  fact    一条经核对的事实：内容 + 原文引文 + 来源 + 页码
  avoid   一条负面规则：什么情况下不要怎么写（从被删的句子里提炼）
  refuse  一条"查过了，没有"：省掉下次重复搜的成本

关键约束 **只有通过校验的内容才准进库**：
  · fact 只从报告的 Evidence 段里取，且必须带 [✓] 和来源
  · 报告正文（Background / Key findings / 摘要）**一个字都不进**
  · 表格碎片、标题、纯符号串一律丢弃

用法：
    python vector_kb.py add <报告.md>            从报告导入（只要事实）
    python vector_kb.py add-log <运行日志>        从日志导入（被删句子 → 负面规则）
    python vector_kb.py rule "触发情形" "该怎么做"   手工加一条规则（最值钱）
    python vector_kb.py search "问题"            查库里有什么
    python vector_kb.py check "一段草稿"          检查草稿是否踩了已有规则
    python vector_kb.py list [--all]             列出全部规则
    python vector_kb.py retire <id>              撤销一条（软删除，可恢复）
    python vector_kb.py stats                    统计
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

KB_DIR = Path(os.environ.get("DEEPDIVE_RULE_DIR", str(Path(__file__).resolve().parent / "规则库")))
DB_PATH = KB_DIR / "rules.db"

# ---------------------------------------------------------------- 相似度

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}")

# 高频虚词二元组，参与匹配只会添噪声
_STOP = {
    "的是", "什么", "怎么", "如何", "可以", "一个", "这个", "那个", "我们", "他们",
    "因为", "所以", "但是", "而且", "以及", "或者", "如果", "就是", "不是", "没有",
    "进行", "通过", "利用", "对于", "关于", "中的", "上的", "了的", "是一", "有以",
}


def tokens(text: str) -> set[str]:
    """中英混排的分词：英文取词，中文取二元组（无需分词器，对短文本够用）。"""
    text = (text or "").lower()
    out = {w for w in _LATIN.findall(text) if len(w) >= 2}
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.add(run)
            continue
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in _STOP:
                out.add(bigram)
    return out


def similarity(a: set[str], b: set[str]) -> float:
    """token 集合的余弦（= 交集 / 几何平均），0~1。"""
    if not a or not b:
        return 0.0
    return len(a & b) / ((len(a) * len(b)) ** 0.5)


# ---------------------------------------------------------------- 存储

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- fact / avoid / refuse
    subject     TEXT NOT NULL DEFAULT '',  -- 主题（来自报告标题或用户输入）
    trigger     TEXT NOT NULL,          -- 什么时候适用
    action      TEXT NOT NULL,          -- 该怎么做 / 不要怎么做
    evidence    TEXT NOT NULL DEFAULT '',  -- 原文引文（fact 必有）
    source      TEXT NOT NULL DEFAULT '',  -- 文件路径或 URL
    page        INTEGER,
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_rules_status ON rules(status, kind);
"""


def connect() -> sqlite3.Connection:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _rid(kind: str, trigger: str, action: str) -> str:
    key = f"{kind}|{_norm(trigger)}|{_norm(action)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


# ---------------------------------------------------------------- 入库闸门

# 明显是排版残留、不是知识的东西
_NOISE_PATTERNS = [
    re.compile(r"^\|"),                 # markdown 表格
    re.compile(r"^#{1,6}\s"),           # 标题
    re.compile(r"^_{1,2}.*_{1,2}$"),    # 斜体说明行（"5/5 claims are..."）
    re.compile(r"^(Sources?|Evidence|References?)\b", re.I),
    re.compile(r"^-{3,}$"),
    # 例题/表格里的残句。旧模型特别爱把这些抄成"事实"：
    #   "The correct answer is 24."  "Baseline: improving ..."
    re.compile(
        r"^(Baseline|Table|Figure|Note|Step|Example|Question|Answer|"
        r"The correct answer|正确答案|答案|表|图)\s*[:：\.]",
        re.I,
    ),
    re.compile(r"correct answer is", re.I),
]

# 至少要有这么多实词才算一条结论，防止"答案：24"这类碎片入库
_MIN_CJK_CHARS = 10
_MIN_LATIN_WORDS = 7


def _is_noise(text: str) -> str | None:
    """返回拒绝原因，None 表示可以入库。"""
    t = (text or "").strip()
    if len(t) < 12:
        return "太短，不像一条结论"
    if not (_CJK_RUN.search(t) or _LATIN.search(t)):
        return "没有实际文字"
    for pat in _NOISE_PATTERNS:
        if pat.search(t):
            return "排版残留或例题残句"
    # 表格行残留：大量数字与短横，几乎没有中文
    if len(re.findall(r"[\d\.\-]", t)) > len(t) * 0.4:
        return "表格碎片，不是结论"
    # 内容量下限：中文按字数，英文按词数
    # 注意：必须累加每个片段的长度，不能只数片段个数 ——
    # 「多层平壁的总热阻…，这是…」会被逗号切成 2 个片段，数个数就误判了。
    cjk_chars = sum(len(run) for run in _CJK_RUN.findall(t))
    latin_words = len(re.findall(r"[A-Za-z]{2,}", t))
    if cjk_chars < _MIN_CJK_CHARS and latin_words < _MIN_LATIN_WORDS:
        return "信息量太少（像半句话或表格单元格）"
    # 连续分隔符，典型的表格/列对齐残留
    if len(re.findall(r"(\s[-–—]\s){2,}", t)) >= 1:
        return "表格碎片，不是结论"
    return None


# ---------------------------------------------------------------- 导入报告

_CLAIM_HEAD = re.compile(r"^\*\*(\d+)\.\*\*\s*(.+)$")
_CITE_LINE = re.compile(r"^\s*[-•]\s*\[(✓|✗)\]\s*(.+)$")
_PAGE = re.compile(r"p\.(\d+)")
_QUOTE = re.compile(r"^\s*[>▌]\s*(.+)$")


def parse_evidence(markdown: str) -> list[dict]:
    """只从 ## Evidence 段抽事实。正文、Sources 段一律不进库。"""
    lines = markdown.splitlines()

    # 定位 Evidence 段
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^##\s+Evidence\s*$", line.strip()):
            start = i + 1
            break
    if start is None:
        return []

    blocks: list[dict] = []
    cur: dict | None = None
    for line in lines[start:]:
        if line.strip().startswith("## "):  # 到下一节就停
            break
        head = _CLAIM_HEAD.match(line)
        if head:
            if cur:
                blocks.append(cur)
            cur = {"text": head.group(2).strip(), "cites": [], "quote": ""}
            continue
        if cur is None:
            continue
        cite = _CITE_LINE.match(line)
        if cite:
            grounded = cite.group(1) == "✓"
            rest = cite.group(2)
            page_match = _PAGE.search(rest)
            cur["cites"].append(
                {
                    "grounded": grounded,
                    "source": _clean_source(rest),
                    "page": int(page_match.group(1)) if page_match else None,
                }
            )
            continue
        quote = _QUOTE.match(line)
        if quote and not cur["quote"]:
            cur["quote"] = quote.group(1).strip()
    if cur:
        blocks.append(cur)
    return blocks


_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BACKTICK = re.compile(r"`([^`]+)`")


def _clean_source(text: str) -> str:
    """从 "`E:\\a.pdf` · p.2" 或 "[标题](url) · p.2" 里取出真正的来源。"""
    text = _PAGE.sub("", text).replace("·", " ")
    m = _BACKTICK.search(text)
    if m:
        return m.group(1).strip()
    m = _MD_LINK.search(text)
    if m:
        return m.group(2).strip()
    return text.strip(" ·")


def import_report(
    path: str | Path, subject: str = "", *, dry_run: bool = False, force: bool = False
) -> dict:
    p = Path(path)
    if not p.exists():
        return {"error": f"文件不存在：{p}"}
    markdown = p.read_text(encoding="utf-8", errors="replace")

    # 闸门：质检不过的报告，一条都不许进库。
    # 这一条比任何清洗规则都重要 —— 脏报告里的 "证据" 格式完全合法，
    # 只有质检能看出它其实是反爬页面上的按钮文字。
    try:
        from qc_gate import check_report_quality

        ok, reason = check_report_quality(str(p))
        if not ok and not force:
            return {
                "拒绝导入": reason,
                "读到的证据条数": 0,
                "入库": 0,
                "说明": "质检未通过的报告不进规则库；修好报告或确认为误判时加 --force",
            }
        gate_note = "质检通过" if ok else "质检未通过（--force 强制导入）"
    except ImportError:
        gate_note = "未找到 qc_gate，跳过质检"

    if not subject:
        m = re.search(r"^#\s+(.+)$", markdown, re.M)
        subject = m.group(1).strip()[:120] if m else p.stem

    blocks = parse_evidence(markdown)
    stats = {"质检": gate_note, "读到的证据条数": len(blocks), "入库": 0, "跳过": 0, "原因": {}}
    conn = connect()
    try:
        for block in blocks:
            text = block["text"]
            reason = _is_noise(text)
            if reason:
                stats["跳过"] += 1
                stats["原因"][reason] = stats["原因"].get(reason, 0) + 1
                continue

            good = [c for c in block["cites"] if c["grounded"] and c["source"]]
            if not good:
                stats["跳过"] += 1
                stats["原因"]["没有可信来源"] = stats["原因"].get("没有可信来源", 0) + 1
                continue

            cite = good[0]
            row = (
                _rid("fact", subject, text),
                "fact",
                subject,
                subject,
                text,
                block["quote"],
                cite["source"],
                cite["page"],
                datetime.now().isoformat(timespec="seconds"),
            )
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO rules"
                    " (id,kind,subject,trigger,action,evidence,source,page,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    row,
                )
            stats["入库"] += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    stats["dry_run"] = dry_run
    return stats


# ---------------------------------------------------------------- 从日志导入负面规则

_KV = re.compile(r"(\w+)=('([^']*)'|\"([^\"]*)\"|(\S+))")


def import_log(path: str | Path, *, dry_run: bool = False) -> dict:
    """把校验器删掉的句子转成"不要这样写"的规则。

    这是整个库里最值钱的一类：它记录的不是知识，是**犯过的错**。
    """
    p = Path(path)
    if not p.exists():
        return {"error": f"文件不存在：{p}"}
    text = p.read_text(encoding="utf-8", errors="replace")

    stats = {"扫到的拒绝记录": 0, "入库": 0, "跳过": 0}
    conn = connect()
    try:
        for line in text.splitlines():
            if "sentence_rejected" not in line:
                continue
            fields = {}
            for m in _KV.finditer(line):
                fields[m.group(1)] = m.group(3) or m.group(4) or m.group(5) or ""
            reason = fields.get("reason", "").strip()
            sentence = fields.get("text", "").strip()
            heading = fields.get("heading", "").strip()
            if not reason or not sentence:
                stats["跳过"] += 1
                continue
            stats["扫到的拒绝记录"] += 1

            action = f"不要写「{sentence[:60]}」这一类句子"
            trigger = f"{heading} 章节" if heading else "报告正文"
            row = (
                _rid("avoid", trigger, action + reason),
                "avoid",
                heading,
                trigger,
                action,
                f"被删原因：{reason}",
                str(p),
                None,
                datetime.now().isoformat(timespec="seconds"),
            )
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO rules"
                    " (id,kind,subject,trigger,action,evidence,source,page,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    row,
                )
            stats["入库"] += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return stats


# ---------------------------------------------------------------- 手工加规则

def add_rule(trigger: str, action: str, *, kind: str = "avoid", source: str = "手工") -> dict:
    """最直接的一条：什么情况下，该怎么做。

    例：add_rule("中文提问", "正文必须用中文，不许中英混排")
    """
    if not trigger.strip() or not action.strip():
        return {"error": "触发情形和做法都不能为空"}
    conn = connect()
    try:
        code = conn.execute(
            "INSERT OR IGNORE INTO rules"
            " (id,kind,subject,trigger,action,evidence,source,page,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                _rid(kind, trigger, action),
                kind,
                "",
                trigger.strip(),
                action.strip(),
                "",
                source,
                None,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return {"入库": code.rowcount, "id": _rid(kind, trigger, action)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 检索

def all_rules(kind: str | None = None) -> list[sqlite3.Row]:
    conn = connect()
    try:
        sql = "SELECT * FROM rules WHERE status='active'"
        args: tuple = ()
        if kind:
            sql += " AND kind=?"
            args = (kind,)
        return conn.execute(sql + " ORDER BY created_at DESC", args).fetchall()
    finally:
        conn.close()


def search(query: str, k: int = 5, min_score: float = 0.12) -> list[dict]:
    """按 token 重叠找相关规则。分数过低就当没找到——宁缺勿滥。"""
    q = tokens(query)
    scored = []
    for row in all_rules():
        blob = tokens(f"{row['trigger']} {row['action']} {row['subject']}")
        score = similarity(q, blob)
        if score >= min_score:
            scored.append((score, row))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "score": round(score, 3),
            "id": row["id"],
            "kind": row["kind"],
            "trigger": row["trigger"],
            "action": row["action"],
            "evidence": row["evidence"],
            "source": row["source"],
            "page": row["page"],
        }
        for score, row in scored[:k]
    ]


def check(draft: str, k: int = 3) -> list[dict]:
    """逐句检查草稿有没有踩到已有规则。"""
    sentences = [s.strip() for s in re.split(r"[。！？\n]+", draft) if len(s.strip()) > 8]
    hits = []
    for sentence in sentences:
        matched = [r for r in search(sentence, k=k) if r["kind"] == "avoid"]
        if matched:
            hits.append({"句子": sentence[:80], "命中规则": matched})
    return hits


# ---------------------------------------------------------------- 命令行

def _print_rules(rows, limit: int = 20) -> None:
    if not rows:
        print("（空）")
        return
    for row in rows[:limit]:
        page = f" · p.{row['page']}" if row["page"] else ""
        print(f"\n[{row['id']}] {row['kind']}  「{row['trigger']}」")
        print(f"    {row['action'][:160]}")
        if row["evidence"]:
            print(f"    依据：{row['evidence'][:110]}")
        print(f"    出处：{row['source'][:90]}{page}")
    if len(rows) > limit:
        print(f"\n…… 还有 {len(rows) - limit} 条，用 list --all 看全部")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    action = args[0]

    if action == "add" and len(args) >= 2:
        subject = args[2] if len(args) > 2 else ""
        stats = import_report(
            args[1], subject, dry_run="--dry-run" in args, force="--force" in args
        )
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    elif action == "add-log" and len(args) >= 2:
        stats = import_log(args[1], dry_run="--dry-run" in args)
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    elif action == "rule" and len(args) >= 3:
        print(json.dumps(add_rule(args[1], args[2]), ensure_ascii=False))

    elif action == "search" and len(args) >= 2:
        found = search(" ".join(args[1:]))
        if not found:
            print("库里没有相关内容 —— 这本身是个有用的结论，别硬凑。")
        for item in found:
            print(f"\n[{item['score']}] {item['kind']}  「{item['trigger']}」")
            print(f"    {item['action'][:160]}")
            if item["evidence"]:
                print(f"    依据：{item['evidence'][:110]}")
            page = f" · p.{item['page']}" if item["page"] else ""
            print(f"    出处：{item['source'][:90]}{page}")

    elif action == "check" and len(args) >= 2:
        hits = check(" ".join(args[1:]))
        if not hits:
            print("没踩到已有规则。")
        for hit in hits:
            print(f"\n⚠ {hit['句子']}")
            for rule in hit["命中规则"]:
                print(f"    [{rule['score']}] {rule['action'][:120]}")

    elif action == "list":
        rows = all_rules()
        if "--all" not in args:
            rows = rows[:20]
        _print_rules(rows, limit=len(rows))

    elif action == "retire" and len(args) >= 2:
        conn = connect()
        try:
            n = conn.execute(
                "UPDATE rules SET status='retired' WHERE id=?", (args[1],)
            ).rowcount
            conn.commit()
        finally:
            conn.close()
        print(f"{'已撤销' if n else '没找到'} {args[1]}（数据还在，可手工改回 active）")

    elif action == "stats":
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT kind, status, COUNT(*) FROM rules GROUP BY kind, status"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            print("规则库还是空的。")
            return
        print(f"规则库：{DB_PATH}\n")
        for kind, status, n in rows:
            print(f"  {kind:8s} {status:8s} {n} 条")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
