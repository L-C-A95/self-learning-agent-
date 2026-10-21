"""半夜无人值守跑一轮，跑完把结果推到手机。

和 explore.py 的分工：explore 只管"怎么读"，这里管"什么时候读、
读之前确保环境就绪、读完怎么通知"。这样 explore 可以随时手动跑，
不受定时任务影响。

四个必须处理的问题，缺一个这套就撑不了几周：

  1. ollama 没起来      —— 自己拉起来，等它就绪
  2. 电脑睡眠           —— 运行期间阻止系统睡眠
  3. 进程重复启动       —— 加锁，只让一个跑
  4. 追线节奏           —— 每天都追会让一条线 8 天就收，且 arXiv 一天
                          新增不了几篇，词频天天比等于没比。所以按
                          "同一批线至少隔 3 天再追"来控制。

    python nightly.py                正常跑一次
    python nightly.py --dry          只检查环境和节奏，不真跑
    python nightly.py --no-push      跑但不推送
    python nightly.py --gap 7        追线间隔改成 7 天
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = Path(__file__).resolve().parent
PY = sys.executable
LOG_DIR = WORK / "logs" / "nightly"  # 用 ASCII 目录名：bat 里的中文路径在 cmd 下不稳
LOCK = WORK / ".nightly.lock"
TRACKS = WORK / "追踪线.json"
PORT = 8765


def _open_log(path: Path):
    """新建的日志带 BOM，Windows 记事本双击打开才不乱码。

    注意不能直接给已存在的文件用 utf-8-sig —— append 模式下 BOM 会被写到
    文件中间，反而更糟。所以只对空文件/新文件用。
    """
    fresh = not path.exists() or path.stat().st_size == 0
    return open(path, "a", encoding="utf-8-sig" if fresh else "utf-8")


def log(msg: str, fh=None) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line)
    if fh:
        fh.write(line + "\n")
        fh.flush()


# ---------------------------------------------------------------- 单实例

def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def take_lock() -> bool:
    if LOCK.exists():
        try:
            info = json.loads(LOCK.read_text(encoding="utf-8"))
            pid = int(info.get("pid", 0))
            if pid and _alive(pid):
                print(f"已经有一个在跑（pid {pid}），这次不重复启动")
                return False
        except Exception:
            pass
    LOCK.write_text(
        json.dumps({"pid": os.getpid(), "at": datetime.now().isoformat()}), encoding="utf-8"
    )
    return True


def drop_lock() -> None:
    try:
        LOCK.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- 环境

def ollama_up() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_ollama(fh) -> bool:
    if ollama_up():
        return True
    log("ollama 没在跑，拉起来", fh)
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        log("找不到 ollama 命令，PATH 里没有。先开着 ollama 再跑", fh)
        return False
    for _ in range(20):
        time.sleep(3)
        if ollama_up():
            log("ollama 就绪", fh)
            return True
    log("ollama 20 次探测都没起来，放弃这一轮", fh)
    return False


def keep_awake() -> None:
    """运行期间不让系统睡着。ES_CONTINUOUS 一直有效，直到进程退出。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:
        pass


def ensure_server(fh) -> None:
    """手机点链接要有服务在跑。没在跑就后台拉一个，跑了多久都留着。"""
    with socket.socket() as s:
        s.settimeout(1.5)
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            return
    log(f"浏览页没在跑，后台启动（{PORT}）", fh)
    try:
        subprocess.Popen(
            [PY, str(WORK / "serve.py"), "--port", str(PORT)],
            cwd=str(WORK),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000008,
            close_fds=True,
        )
        time.sleep(2)
    except Exception as exc:
        log(f"启动浏览页失败：{exc}", fh)


# ---------------------------------------------------------------- 节奏

def days_since(date_str: str) -> int | None:
    try:
        return (datetime.now() - datetime.strptime(date_str, "%Y-%m-%d")).days
    except Exception:
        return None


def tracks_due(gap: int) -> tuple[bool, str]:
    """该不该推进追踪线。返回 (要不要追, 原因)。"""
    if not TRACKS.exists():
        return False, "还没有追踪线，这轮先自由探索找线索"
    try:
        state = json.loads(TRACKS.read_text(encoding="utf-8"))
    except Exception:
        return False, "追踪线文件读不了，先自由探索"

    act = [t for t in state.get("tracks", []) if t.get("status") == "active"]
    if not act:
        return False, "没有活跃的线，这轮自由探索"

    due = []
    for t in act:
        d = days_since(t.get("last_run") or "")
        if d is None or d >= gap:
            due.append(f"{t['id']}({(d if d is not None else '从未')}天前)")
    if due:
        return True, f"{len(due)} 条线到期：{', '.join(due)}"
    return False, f"{len(act)} 条线都还没到 {gap} 天，这轮只做自由探索"


# ---------------------------------------------------------------- 主

def run_explore(rounds: int, minutes: int, papers: int, fh) -> int:
    cmd = [
        PY,
        "-X",
        "utf8",
        str(WORK / "explore.py"),
        "--rounds",
        str(rounds),
        "--papers",
        str(papers),
        "--minutes",
        str(minutes),
    ]
    log(f"开始：{' '.join(cmd[2:])}", fh)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    with _open_log(LOG_DIR / "explore_output.log") as out:
        out.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M} {' '.join(cmd[2:])} =====\n")
        out.flush()
        proc = subprocess.run(
            cmd, cwd=str(WORK), stdout=out, stderr=subprocess.STDOUT, env=env,
            timeout=minutes * 60 + 900,
        )
    log(f"结束，返回码 {proc.returncode}", fh)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="无人值守跑一轮并推送到手机")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--minutes", type=int, default=35)
    ap.add_argument("--papers", type=int, default=2)
    ap.add_argument("--gap", type=int, default=3, help="同一条线至少隔几天再追（默认 3）")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--dry", action="store_true", help="只检查，不跑不推")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    with _open_log(LOG_DIR / f"{stamp}.log") as fh:
        fh.write(f"\n\n===== nightly {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")

        if not take_lock():
            return 0
        try:
            keep_awake()

            if not ensure_ollama(fh):
                if not args.no_push:
                    import notify

                    notify.send("AI 追踪 · 这轮没跑起来", "ollama 起不来，凌晨那轮跳过了。", "")
                return 2

            due, why = tracks_due(args.gap)
            log(f"节奏：{why}", fh)

            if args.dry:
                rounds = args.rounds if due else 1
                log(f"（--dry）会跑 --rounds {rounds}", fh)
                return 0

            if not args.no_server:
                ensure_server(fh)

            rounds = args.rounds if due else 1
            code = run_explore(rounds, args.minutes, args.papers, fh)

            if code != 0:
                log(f"explore 返回非 0，看 {LOG_DIR.name}/explore_output.log", fh)

            if not args.no_push:
                import notify

                title, body, url = notify.digest()
                stale = True
                newest = notify._newest(1)
                if newest:
                    mtime = datetime.fromtimestamp(newest[0].stat().st_mtime)
                    stale = mtime.date() != datetime.now().date()
                if stale:
                    # 什么都没读到的时候，绝不能把昨天的报告当今天的推出去。
                    # 宁可推一句"这轮空手而归"，也别让人以为有新东西。
                    title = "AI 追踪 · 这轮没读到新的"
                    body = (
                        "夜里跑完了，但没有新增报告。多半是 arXiv 限流，"
                        "或者几条线都已经读到最前面、暂时没有新论文。\n"
                        "明天再试，不用管。"
                    )
                    url = ""
                elif code != 0:
                    title = title + "（有报错）"
                for name, ok, note in notify.send(title, body, url):
                    log(f"推送 {name}：{'成功' if ok else '失败 ' + note}", fh)

            return 0 if code == 0 else 1
        finally:
            drop_lock()


if __name__ == "__main__":
    sys.exit(main())
