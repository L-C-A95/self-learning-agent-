"""追踪线的判据：没有历史就不许谈趋势。

2026-10-19 立的规矩：判断"某个方向在变热"要时间序列。只有一期的时候
说"这个方向在升温"，和拍脑袋没有区别。所以 shift() 在不足两期时必须返回空。

    D://anaconda3//python.exe tests\\test_tracks.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tracks as tk  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


def paper(pid: str, title: str, summary: str = "") -> dict:
    return {
        "id": pid,
        "title": title,
        "summary": summary,
        "year": "2025",
        "url": f"https://arxiv.org/abs/{pid}",
        "points": [{"point": "做到 95% 效率", "quote": "", "page": 1}],
        "terms": [],
    }


# ---------------------------------------------------------------- 词频

counts = tk.term_counts([paper("a", "Surface Code Logical Qubit", "surface code logical qubit error rate")])
check("英文词按出现次数计", counts.get("surface") == 2, str(counts))
check("只出现一次的词不进表", "error" not in counts, str(counts))
check("停用词被过滤", "the" not in counts and "and" not in counts, str(counts))
check("短于 2 个字的噪声被过滤", all(len(w) >= 2 for w in counts), str(counts))


# ---------------------------------------------------------------- 趋势判据

one = {"history": [{"terms": {"surface": 6}}]}
check("只有一期时不许谈趋势", tk.shift(one) == [], "第一期就报变化")

two = {
    "history": [
        {"terms": {"surface": 6, "decoder": 1, "code": 4}},
        {"terms": {"surface": 6, "decoder": 5, "code": 4}},
    ]
}
moves = {m["word"]: m for m in tk.shift(two)}
check("涨上去的词被认出", moves.get("decoder", {}).get("kind") == "up", str(moves))
check("持平的词不算变化", "surface" not in moves, str(moves))
check("掉下去的词不进涨幅榜", "code" not in moves, str(moves))

newbie = {"history": [{"terms": {"surface": 6}}, {"terms": {"surface": 6, "tensor": 3}}]}
check("新出现的词标为 new", any(m["word"] == "tensor" and m["kind"] == "new" for m in tk.shift(newbie)))

faded = tk.fade({"history": [{"terms": {"surface": 6, "decoder": 5}}, {"terms": {"surface": 6}}]})
check("消失的词被认出", "decoder" in faded, str(faded))
check("还在的词不算消失", "surface" not in faded, str(faded))

# 两期语料还是同一批论文时，"词频没变"是理所当然的，要能识别出来
check("同一批语料重合度为 1", tk._overlap([{"corpus": ["a", "b"]}, {"corpus": ["a", "b"]}]) == 1.0)
check("语料全换重合度为 0", tk._overlap([{"corpus": ["a", "b"]}, {"corpus": ["c", "d"]}]) == 0.0)
check("没存语料就不下判断", tk._overlap([{"corpus": []}, {"corpus": ["c"]}]) is None)
check("只有一期没有重合度", tk._overlap([{"corpus": ["a"]}]) is None)

# ---------------------------------------------------------------- 线的一生

tmp = Path(tempfile.mkdtemp())
tk.STATE = tmp / "追踪线.json"
tk.REPORT_DIR = tmp / "周报"

state = {"tracks": [], "n": 0}
track = tk.create(state, "物理 · 量子信息", {"subtopic": "表面码量子纠错", "query": "surface code"},
                  {"field": "物理", "papers": [paper("a1", "Surface Code Logical Qubit")], "learned": 1})
check("建线后计第 1 周", track["week"] == 1, str(track["week"]))
check("建线后记下读过的论文", track["read_ids"] == ["a1"], str(track["read_ids"]))

tk.advance(track, {"field": "物理", "papers": [paper("a2", "Surface Code Decoder"), paper("a1", "Surface Code")]})
check("第二期周次加到 2", track["week"] == 2, str(track["week"]))
check("读过的论文不重复记", track["read_ids"] == ["a1", "a2"], str(track["read_ids"]))
check("两期后才有历史可比", len(track["history"]) == 2)

# 追满 8 周自动收线
for _ in range(6):
    tk.advance(track, {"field": "物理", "papers": [paper("x", "Surface Code")]})
check("满 8 周自动收线", track["status"] == "done", f"week={track['week']} status={track['status']}")

# 收线后不再被挑中
state2 = {"tracks": [track], "n": 1}
check("收掉的线不再派活", tk.pick(state2) == [], str([t["id"] for t in tk.pick(state2)]))

# 报告文件名不能带斜杠之类
path = tk.write_report(track, {"papers": [paper("z", "Surface Code")]}, [])
check("报告写进目录且文件名安全", path.exists() and "/" not in path.name and "\\" not in path.name, str(path))

print(f"\n总计：通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
