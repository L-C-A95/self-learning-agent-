"""无人值守研究循环 —— 但它只负责"提问"，不负责"记住"。

和前一版的三个差别：

1. **不再从旧报告里抽"研究笔记"**。旧版把报告正文当上下文喂给模型，等于
   让排版垃圾当灵感来源，于是它反复问同一类问题。现在只喂两类东西：
   已经过校验的事实（fact），和犯过的错（avoid）。

2. **去重改成真的去重**。旧版判断是
       if any(new_question in t or t in new_question for t in topics)
   拿一个完整疑问句去和主题词比子串，几乎永不命中，所以 08:54 问完
   09:26 又问了一遍。现在用 token 重叠，阈值 0.5。

3. **问之前先查库**。库里如果已经有很接近的答案，就不搜了 —— 这是省时间，
   也是"学会"最朴素的一种表现。

能不能进库，仍然由 `vector_kb.py` 的闸门决定，这里不碰。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

WORK_DIR = Path(__file__).resolve().parent
OLLAMA = "http://127.0.0.1:11434/api/generate"
MODEL = os.environ.get("DEEPDIVE_MODEL", "qwen3:8b")
ASKED_FILE = Path(os.environ.get("DEEPDIVE_RULE_DIR", str(Path(__file__).resolve().parent / "规则库"))) / "asked.json"
AUTO_RESEARCH = WORK_DIR / "auto_research.py"

MAX_ROUNDS = int(os.environ.get("LOOP_ROUNDS", "3"))
SLEEP_SECONDS = int(os.environ.get("LOOP_SLEEP", "60"))
DUPLICATE_AT = 0.5  # 与已问过的问题相似度超过这个值，当作重复

sys.path.insert(0, str(WORK_DIR))
from vector_kb import all_rules, search, tokens, similarity  # noqa: E402


# ---------------------------------------------------------------- 记忆

def load_asked() -> list[str]:
    if ASKED_FILE.exists():
        try:
            return json.loads(ASKED_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_asked(questions: list[str]) -> None:
    ASKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ASKED_FILE.write_text(
        json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def known_context(limit: int = 25) -> tuple[list[str], str]:
    """已掌握的事实与犯过的错。只取入库过的，不取报告原文。"""
    facts = [r for r in all_rules("fact")]
    avoids = [r for r in all_rules("avoid")]
    topics = sorted({r["subject"] for r in facts if r["subject"]})
    lines = [f"- {r['action']}" for r in facts[:limit]]
    if avoids:
        lines.append("\n已有教训（不要重犯）：")
        lines += [f"- {r['action']}" for r in avoids[:limit]]
    return topics, "\n".join(lines)


# ---------------------------------------------------------------- 提问

def is_duplicate(question: str, known: list[str]) -> tuple[bool, str]:
    q = tokens(question)
    for item in known:
        score = similarity(q, tokens(item))
        if score >= DUPLICATE_AT:
            return True, f"{item[:50]}（相似度 {score:.2f}）"
    return False, ""


def generate_question(topics: list[str], context: str, avoid: list[str]) -> str | None:
    print("🤔 正在决定下一个要查什么……", flush=True)
    prompt = f"""你是一个严谨的研究员。已研究过的主题：
{', '.join(topics) or '（暂无）'}

已确认的事实（每条都有出处，不要重复推这些）：
{context or '（暂无）'}

已经问过、不必再问的问题：
{chr(10).join('- ' + q for q in avoid[-10:]) or '（暂无）'}

请提出**一个**尚未解决的具体问题。
要求：
1. 必须是具体疑问句，带上限定条件（方法名、场景、变量）。
2. 禁止宏观问题（如"AI 的未来是什么"）。
3. 不能与上面已问过的问题重复或近似。
4. 只输出问题本身，不要解释、不要引号、不要编号。"""

    try:
        resp = requests.post(
            OLLAMA, json={"model": MODEL, "prompt": prompt, "stream": False}, timeout=180
        )
        resp.raise_for_status()
        text = (resp.json().get("response") or "").strip()
        text = re.sub(r"^[\d\.\-\*\s\"“”『』]+", "", text).replace("\n", " ").strip()
        return text[:160] or None
    except Exception as exc:
        print(f"生成问题失败：{type(exc).__name__}: {exc}", flush=True)
        return None


# ---------------------------------------------------------------- 主循环

def main() -> None:
    print("=== 自主研究循环（只提问，不擅自记东西）===", flush=True)

    for round_num in range(1, MAX_ROUNDS + 1):
        print(f"\n{'#' * 50}\n第 {round_num}/{MAX_ROUNDS} 轮\n{'#' * 50}", flush=True)

        topics, context = known_context()
        asked = load_asked()

        if not topics and not context:
            print("规则库还是空的 —— 先手工跑一次研究，让第一条事实进来。", flush=True)
            return

        question = generate_question(topics, context, asked)
        if not question:
            print("没生成有效问题，本轮跳过。", flush=True)
            time.sleep(SLEEP_SECONDS)
            continue

        print(f"\n💡 本轮问题：【{question}】", flush=True)

        dup, why = is_duplicate(question, asked + topics)
        if dup:
            print(f"⚠️ 与已有内容重复，跳过：{why}", flush=True)
        else:
            # 库里已有接近的答案就不再搜，省时间也省得重复囤积
            hits = [h for h in search(question, k=3) if h["score"] >= 0.5 and h["kind"] == "fact"]
            if hits:
                print(f"📚 库里已有高相关结论（{hits[0]['score']}）：{hits[0]['action'][:70]}", flush=True)
                print("   本轮不重复研究。", flush=True)
            else:
                print("\n🚀 开始研究……", flush=True)
                subprocess.run([sys.executable, str(AUTO_RESEARCH), question], cwd=str(WORK_DIR))

            asked.append(question)
            save_asked(asked)

        if round_num < MAX_ROUNDS:
            print(f"\n⏳ 休息 {SLEEP_SECONDS} 秒……", flush=True)
            time.sleep(SLEEP_SECONDS)

    print("\n=== 轮次结束，安全停止 ===", flush=True)


if __name__ == "__main__":
    main()
