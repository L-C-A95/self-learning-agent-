"""DeepDive 使用入口 —— 不需要记命令，选数字就行。

    python run.py                    菜单模式
    python run.py book  "多层平壁的总热阻如何计算"    查教材
    python run.py paper "How does DPO work?"          查文献（论文）
    python run.py web   "AI alignment 2026"           查网页
    python run.py find  "thermal resistance"          自己去 arXiv 找论文
    python run.py rules                               规则库
    python run.py index book  "D:\\教材"              建/更新教材库
    python run.py index paper "D:\\论文"              建/更新文献库

三个库的区别：
  book  = 教材，中文，离线，每条结论带页码
  paper = 论文 PDF，同样离线带页码，但要求 PDF 有文字层
  web   = 上网搜，会过质检闸门，来源质量看运气
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:  # Windows 控制台默认不是 UTF-8，中文会变问号
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK_DIR = Path(__file__).resolve().parent
DEEPDIVE = Path(r"D:\anaconda3\Scripts\deepdive.exe")
PYTHON = Path(sys.executable)
MODEL = os.environ.get("DEEPDIVE_MODEL", "qwen3:8b")
EMBED_MODEL = os.environ.get("DEEPDIVE_EMBED", "bge-m3")

BOOK_DIR = Path(os.environ.get("DEEPDIVE_BOOK_DIR", str(Path(__file__).resolve().parent / "教材库")))
PAPER_DIR = Path(os.environ.get("DEEPDIVE_PAPER_DIR", str(Path(__file__).resolve().parent / "文献库")))
BOOK_INDEX = Path(os.environ.get("DEEPDIVE_BOOK_INDEX", str(BOOK_DIR / "index.db")))
PAPER_INDEX = Path(os.environ.get("DEEPDIVE_PAPER_INDEX", str(PAPER_DIR / "index.db")))

LIBS = {
    "book": ("教材库", BOOK_DIR, BOOK_INDEX),
    "paper": ("文献库", PAPER_DIR, PAPER_INDEX),
}

SUPPORTED = {".pdf", ".md", ".markdown", ".txt", ".rst", ".html", ".htm"}
QA_DIR = WORK_DIR / "产出" / "问答记录"
NO_CLAIMS = ("No verifiable claims could be extracted", "没有从资料里提取到可核对的结论")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _run(cmd: list[str], cwd: Path = WORK_DIR) -> int:
    print("\n命令：" + " ".join(cmd) + "\n", flush=True)
    return subprocess.run(cmd, cwd=str(cwd)).returncode


def index_stats(index_path: Path) -> tuple[int, int]:
    """(文档数, 片段数)。库存在但为空是最容易骗过检查的状态。"""
    if not index_path.exists():
        return 0, 0
    try:
        conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        finally:
            conn.close()
        return int(docs), int(chunks)
    except sqlite3.Error:
        return 0, 0


def scan_folder(folder: Path) -> list[Path]:
    return [
        p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
    ]


def rule_stats() -> tuple[int, int]:
    """(事实条数, 教训条数)。规则库才是"学到东西"的地方。"""
    db = Path(os.environ.get("DEEPDIVE_RULE_DIR", str(Path(__file__).resolve().parent / "规则库"))) / "rules.db"
    if not db.exists():
        return 0, 0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            facts = conn.execute(
                "SELECT COUNT(*) FROM rules WHERE kind='fact' AND status='active'"
            ).fetchone()[0]
            avoids = conn.execute(
                "SELECT COUNT(*) FROM rules WHERE kind='avoid' AND status='active'"
            ).fetchone()[0]
        finally:
            conn.close()
        return int(facts), int(avoids)
    except sqlite3.Error:
        return 0, 0


def ask(kind: str, question: str) -> Path | None:
    """在指定库里提问。注意：不能加 -b 参数，否则会被覆盖回网页检索。"""
    label, folder, index_path = LIBS[kind]
    docs, chunks = index_stats(index_path)
    if chunks == 0:
        print(f"\n{label}还是空的（{docs} 个文档 / {chunks} 个片段）。")
        print(f"先按 4，把放 PDF 的文件夹加进{label}。")
        return None

    QA_DIR.mkdir(parents=True, exist_ok=True)
    out = QA_DIR / f"问答_{label}_{_stamp()}.md"
    code = _run(
        [
            str(DEEPDIVE), "research", question,
            "--corpus", str(index_path),
            "-m", MODEL,
            "-o", str(out),
            "--force",
        ]
    )
    if code != 0:
        print(f"\n失败，返回码 {code}")
        return None

    text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
    if any(marker in text for marker in NO_CLAIMS):
        print("\n这次没有找到可用证据。常见原因：")
        print(f"  1) 库里没有讲这个问题的内容（当前只有 {chunks} 个片段）—— 这是最常见的原因")
        print("  2) 问题问法太偏 —— 换成书里/论文里的原话再试")
        print("  3) PDF 是扫描件，没有文字层 —— 需要先 OCR")
        print(f"\n文件已生成（内容为空）：{out}")
        return out

    print(f"\n报告已生成：{out}")
    return out


def research_web(topic: str) -> None:
    script = WORK_DIR / "auto_research.py"
    if not script.exists():
        print("找不到 auto_research.py")
        return
    if _run([str(PYTHON), str(script), topic]) != 0:
        print("\n失败。")


def find_papers(query: str) -> None:
    """让程序自己去 arXiv 找论文、下载、建索引。"""
    script = WORK_DIR / "find_papers.py"
    if not script.exists():
        print("找不到 find_papers.py")
        return
    _run([str(PYTHON), str(script), query, "--max", "3", "--index"])


def make_survey(topic: str) -> None:
    """产出一份成果报告：知识点 + 横向对比 + 纵向演进 + 发展方向。"""
    script = WORK_DIR / "survey.py"
    if not script.exists():
        print("找不到 survey.py")
        return
    _run([str(PYTHON), str(script), topic, "--papers", "5"])


def rules_console() -> None:
    """规则库：这里存的是「学到的事实」和「犯过的错」，不是报告正文。"""
    script = WORK_DIR / "vector_kb.py"
    _run([str(PYTHON), str(script), "stats"])
    print("\n  1) 查库里有没有某个话题")
    print("  2) 检查一段草稿有没有踩坑")
    print("  3) 看最近入库的规则")
    print("  0) 返回")
    choice = input("\n选一个数字：").strip()
    if choice == "1":
        q = input("查什么？").strip()
        if q:
            _run([str(PYTHON), str(script), "search", q])
    elif choice == "2":
        q = input("粘一段文字：").strip()
        if q:
            _run([str(PYTHON), str(script), "check", q])
    elif choice == "3":
        _run([str(PYTHON), str(script), "list", "--all"])


def build_index(kind: str, folder: str) -> None:
    label, default_dir, index_path = LIBS[kind]
    target = Path(folder).expanduser()
    if not target.exists():
        print(f"\n目录不存在：{target}")
        return
    if not target.is_dir():
        print(f"\n这是个文件，不是文件夹：{target}")
        return

    files = scan_folder(target)
    if not files:
        print(f"\n在 {target} 里没找到可索引的文件。")
        print("支持的格式：" + "  ".join(sorted(SUPPORTED)))
        return

    print(f"\n{label}：找到 {len(files)} 个文件")
    for f in files[:12]:
        print(f"  · {f.name}")
    if len(files) > 12:
        print(f"  …… 还有 {len(files) - 12} 个")

    index_path.parent.mkdir(parents=True, exist_ok=True)
    code = _run(
        [
            str(DEEPDIVE), "index", str(target),
            "-o", str(index_path),
            "--embed-model", EMBED_MODEL,
        ]
    )
    if code != 0:
        print(f"\n建索引失败，返回码 {code}")
        return

    docs, chunks = index_stats(index_path)
    if chunks == 0:
        print("\n索引是空的 —— 这些文件很可能没有文字层。")
        print("扫描版 PDF（整页图片）需要先做 OCR，否则抽不出任何文字。")
        return
    print(f"\n{label}已更新：{docs} 个文档，{chunks} 个片段。")
    print(f"现在可以选 {1 if kind == 'book' else 2} 提问了。")


def menu() -> None:
    while True:
        b_docs, b_chunks = index_stats(BOOK_INDEX)
        p_docs, p_chunks = index_stats(PAPER_INDEX)
        n_facts, n_avoids = rule_stats()
        print("\n" + "=" * 60)
        print("  DeepDive 入口")
        print("=" * 60)
        print(f"  教材库：{b_docs} 个文档 / {b_chunks} 个片段" if b_chunks else "  教材库：空")
        print(f"  文献库：{p_docs} 个文档 / {p_chunks} 个片段" if p_chunks else "  文献库：空")
        print(f"  规则库：{n_facts} 条事实 / {n_avoids} 条教训")
        print("-" * 60)
        print("  1) 查教材（离线，带页码）")
        print("  2) 查文献 / 论文（离线，带页码）")
        print("  3) 查网页（联网，过质检）")
        print(f"  4) 让它自己找论文（arXiv，模型 {MODEL}）")
        print("  5) 把 PDF 加进库")
        print("  6) 规则库（学到什么 / 查一句话）")
        print("  7) 产出一份成果报告（知识点+横向+纵向+方向）")
        print("  0) 退出")
        choice = input("\n选一个数字：").strip()

        if choice == "1":
            q = input("要查什么？").strip()
            if q:
                ask("book", q)
        elif choice == "2":
            q = input("要查什么？（英文问题效果更好）").strip()
            if q:
                ask("paper", q)
        elif choice == "3":
            t = input("研究什么主题？").strip()
            if t:
                research_web(t)
        elif choice == "4":
            q = input("找什么主题的论文？（英文关键词效果最好）").strip()
            if q:
                find_papers(q)
        elif choice == "5":
            which = input("加进哪个库？1=教材 2=文献：").strip()
            kind = "paper" if which == "2" else "book"
            label, default_dir, _ = LIBS[kind]
            d = input(f"PDF 在哪个文件夹？（直接回车用 {default_dir}）").strip() or str(default_dir)
            build_index(kind, d)
        elif choice == "6":
            rules_console()
        elif choice == "7":
            t = input("要研究哪个主题？（英文原文效果最好）").strip()
            if t:
                make_survey(t)
        elif choice in ("0", "q", ""):
            print("再见。")
            return
        else:
            print("没这个选项。")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        menu()
        return
    action = args[0]
    if action in ("book", "paper") and len(args) > 1:
        ask(action, " ".join(args[1:]))
    elif action == "web" and len(args) > 1:
        research_web(" ".join(args[1:]))
    elif action == "find" and len(args) > 1:
        find_papers(" ".join(args[1:]))
    elif action == "rules":
        rules_console()
    elif action == "index" and len(args) > 2 and args[1] in LIBS:
        build_index(args[1], " ".join(args[2:]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
