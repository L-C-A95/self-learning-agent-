"""推送与网页的判据。

这里测的都是"错了会很难发现"的地方：摘要取错段落（推出去的东西跟报告对不上）、
标题太长被通知栏截断、以及网页服务的路径越界。

    D://anaconda3//python.exe tests\\test_notify.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nightly  # noqa: E402
import notify  # noqa: E402
import serve  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


CFG = {"浏览地址": "http://192.0.2.10:8765", "浏览端口": 8765}

WEEKLY = """# 追踪 · 基于物理信息神经网络的流体动力学方程求解 · 第 3 周

_从 2026-10-19 开始追，累计 8 篇 / 40 条要点_

## 这条线走到哪了

1. 前两周在解决怎么让网络满足物理约束。
2. 这周开始往自动发现方程的方向走。
3. 现在卡在数据稀疏时的稳定性。

## 跟前几周比，什么变了

**新冒出来 / 在涨**

- `discovery` — 这周 6 次（首次出现）
- `sparse` — 这周 4 次（从 1.2 涨上来）

## 这周新读的

### Some Paper
`2026 · https://arxiv.org/abs/1234.5678`
"""

DAILY = """# 每日发现 · 2026-10-19 23:00

_逛了 2 个方向，读到 2 篇，摘出 9 条要点，记进知识库 7 条（重复的不算）_

## 计算机 · 系统与网络

### 1. D-CAST

- 崩溃容错协议用于管理可靠状态复制，防止节点崩溃。
- 无线信道上同时出现崩溃故障和拜占庭故障时，原有协议失效。
- 提出 D-CAST 用信号叠加直接算平均值，省掉互相传话。
"""


def fresh() -> Path:
    return Path(tempfile.mkdtemp(prefix="nt_"))


# ---------------------------------------------------------------- 追踪周报的摘要

root = fresh()
(root / "追踪周报").mkdir()
(root / "追踪周报" / "t01_第3周.md").write_text(WEEKLY, encoding="utf-8")
notify.OUT = root
title, body, url = notify.digest(CFG)

check("标题改写成短形式", title.startswith("追踪第3周 · "), title)
check("标题不超过通知栏宽度", len(title) <= 40, f"{len(title)} 字：{title}")
check(
    "正文取的是「走到哪了」三句",
    all(k in body for k in ("物理约束", "自动发现方程", "数据稀疏")),
    body[:120],
)
check("编号被去掉", "1. 前两周" not in body, body[:60])
check("带上了变化那句", "discovery" in body, body[-90:])
check("链接指向具体文件", "?f=" in url and url.startswith("http://192.0.2.10:8765"), url)

# 正文不能太长，否则通知栏展开也读不完
check("正文被限制长度", len(body) <= notify.MAX_BODY + 200, f"{len(body)} 字")

# ---------------------------------------------------------------- 每日发现的摘要

root2 = fresh()
(root2 / "每日发现").mkdir()
(root2 / "每日发现" / "每日发现_20261019.md").write_text(DAILY, encoding="utf-8")
notify.OUT = root2
title2, body2, _ = notify.digest(CFG)

check("每日发现用导读开头", "逛了 2 个方向" in body2, body2[:60])
check("每日发现取前几条要点", "拜占庭" in body2 or "D-CAST" in body2, body2[:120])
check("每日发现标题不为空", bool(title2.strip()), title2)

# ---------------------------------------------------------------- 没有报告时不瞎编

root3 = fresh()
notify.OUT = root3
t3, b3, u3 = notify.digest(CFG)
check("空目录给出明确提示", ("空" in t3 + b3) or "没有" in t3, f"{t3} / {b3}")
check("空目录不给链接", u3 == "", u3)

# ---------------------------------------------------------------- 节奏判断

TR = fresh() / "追踪线.json"
nightly.TRACKS = TR

check("没有追踪线文件时不追线", nightly.tracks_due(3)[0] is False)

TR.write_text('{"tracks":[]}', encoding="utf-8")
check("没有活跃线时不追线", nightly.tracks_due(3)[0] is False)

TR.write_text(
    '{"tracks":[{"id":"t01","status":"active","last_run":"%s"}]}' % "2026-10-01",
    encoding="utf-8",
)
due, why = nightly.tracks_due(3)
check("久没跑的线到期", due is True, why)

import datetime as _dt  # noqa: E402

today = _dt.date.today()
TR.write_text(
    '{"tracks":[{"id":"t01","status":"active","last_run":"%s"}]}' % today.isoformat(),
    encoding="utf-8",
)
due2, why2 = nightly.tracks_due(3)
check("今天刚跑过的线不到期", due2 is False, why2)

TR.write_text(
    '{"tracks":[{"id":"t01","status":"active","last_run":""}]}', encoding="utf-8"
)
check("从没跑过的线算到期", nightly.tracks_due(3)[0] is True)

# ---------------------------------------------------------------- 网页的路径防护

serve.ROOT = fresh()
(serve.ROOT / "追踪周报").mkdir()
(serve.ROOT / "追踪周报" / "a.md").write_text("# hi", encoding="utf-8")

check("正常路径能解析", serve.resolve("追踪周报/a.md") is not None)
check("上跳一级被拒", serve.resolve("../追踪线.json") is None)
check("跳出到系统盘被拒", serve.resolve("../../Windows/win.ini") is None)
check("绝对路径被收进产出目录", serve.resolve("C:/Windows/win.ini") is None)

# ---------------------------------------------------------------- 选哪份报告

import os  # noqa: E402
import time as _t  # noqa: E402

# 术语表每轮结束都会被重写，mtime 永远最新。它一旦被当成"最新报告"，
# 推送内容就变成术语表的标题——2026-10-19 真实发生过一次。
root4 = fresh()
(root4 / "追踪周报").mkdir()
(root4 / "每日发现").mkdir()
w4 = root4 / "追踪周报" / "t01_第2周.md"
w4.write_text(WEEKLY.replace("第 3 周", "第 2 周"), encoding="utf-8")
d4 = root4 / "每日发现" / "每日发现_20261018.md"
d4.write_text(DAILY, encoding="utf-8")
g4 = root4 / "术语表.md"
g4.write_text("# 术语表\n\n## 交叉 · 科学计算\n", encoding="utf-8")
now = _t.time()
os.utime(w4, (now - 90000, now - 90000))  # 昨天的周报
os.utime(d4, (now - 3600, now - 3600))
os.utime(g4, (now, now))  # 故意让它最新
notify.OUT = root4
t4, b4, _ = notify.digest(CFG)
check("根目录的术语表不会被当成报告", "术语表" not in t4, t4)
check("没有当天周报时退到每日发现", "逛了" in b4, b4[:50])

# 当天有周报时，即使每日发现写得更晚，也优先推周报：
# 推送的重点是「这条线走到哪了」，那三句只在周报里。
root5 = fresh()
(root5 / "追踪周报").mkdir()
(root5 / "每日发现").mkdir()
w5 = root5 / "追踪周报" / "t01_第3周.md"
w5.write_text(WEEKLY, encoding="utf-8")
d5 = root5 / "每日发现" / "每日发现_今日.md"
d5.write_text(DAILY, encoding="utf-8")
now5 = _t.time()
os.utime(w5, (now5 - 600, now5 - 600))
os.utime(d5, (now5, now5))
notify.OUT = root5
t5, b5, _ = notify.digest(CFG)
check("当天有周报就推周报", t5.startswith("追踪第3周"), f"{t5} / {b5[:30]}")
check("推的是周报里的那几句", "数据稀疏" in b5, b5[:60])

print(f"\n总计：通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
