"""把最新一轮的结果推到手机。

为什么需要它：追踪线是半夜跑的，跑完没人看等于没跑。手机上收到一条
「第 3 周 · 这条线卡在哪」，比早上想起来去翻文件夹现实得多。

通道填一个就能用，优先级从高到低：

  pushplus    微信收。pushplus.plus 微信扫码拿 token，国内最稳
  ntfy        手机装 ntfy app 订阅一个话题名。零注册，服务器在海外
  serverchan  Server酱，微信收，需要 SendKey
  email       邮件，需要 SMTP 授权码（QQ/163 都在设置里开）

配置写在 推送配置.json，第一次运行会自动生成。想换通道只改这个文件，
不用动代码。

    python notify.py --test          发一条测试，确认手机上收不收得到
    python notify.py --dry           只打印要推的内容，不真的发
    python notify.py                 把最新报告推一次
    python notify.py --channel ntfy  临时只用某个通道
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import smtplib
import sys
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = Path(__file__).resolve().parent
CFG = WORK / "推送配置.json"
OUT = WORK / "产出"

# 报告可能很长，手机通知只留开头一眼能看完的部分
MAX_BODY = 700


# ---------------------------------------------------------------- 配置

def _default_cfg() -> dict:
    return {
        "启用": ["ntfy"],
        "ntfy": {
            # 话题名等于密码：ntfy.sh 上谁猜到谁就能订阅，所以随机生成。
            # 换手机重装 app 时把这里的话题名重新订阅一次即可。
            "topic": "ai-track-" + secrets.token_hex(4),
            "server": "https://ntfy.sh",
        },
        "pushplus": {"token": ""},
        "serverchan": {"sendkey": ""},
        "email": {
            "host": "smtp.qq.com",
            "port": 465,
            "user": "",
            "password": "",
            "to": "",
        },
        # 手机上点开看全文的地址。留空会自动探测 Tailscale 的 IP
        "浏览地址": "",
        "浏览端口": 8765,
    }


def load_cfg(create: bool = True) -> dict:
    if CFG.exists():
        try:
            cfg = json.loads(CFG.read_text(encoding="utf-8"))
            base = _default_cfg()
            for k, v in base.items():
                cfg.setdefault(k, v)
            return cfg
        except json.JSONDecodeError:
            print(f"  · {CFG.name} 格式坏了，用默认配置")
    cfg = _default_cfg()
    if create:
        CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  · 已生成 {CFG.name}，默认走 ntfy，话题名 {cfg['ntfy']['topic']}")
    return cfg


def browse_base(cfg: dict) -> str:
    """手机上打开报告用的地址。

    Tailscale 的 IP 是固定的（重启、换网络都不变），所以不用每次探测；
    但第一次填之前探一次，省得让人自己去查。
    """
    given = (cfg.get("浏览地址") or "").strip().rstrip("/")
    if given:
        return given
    ip = _tailscale_ip()
    port = cfg.get("浏览端口") or 8765
    return f"http://{ip}:{port}" if ip else ""


def _tailscale_ip() -> str:
    import subprocess

    exe = Path(r"C:\Program Files\Tailscale\tailscale.exe")
    if not exe.exists():
        return ""
    try:
        out = subprocess.run(
            [str(exe), "ip", "-4"], capture_output=True, text=True, timeout=8
        )
        return (out.stdout or "").strip().splitlines()[0].strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 摘要

REPORT_DIRS = ["追踪周报", "每日发现", "成果报告", "夜间记录", "问答记录"]


def _newest(limit: int = 1) -> list[Path]:
    """最新的报告文件。

    只看五个报告子目录，不看根目录：`术语表.md` 每轮结束都会被重写一次
    （mtime 变成最新），`说明.md` 也常被编辑。它们混进来的话，
    推送内容会变成「术语表」的标题，看着像发错了东西。
    """
    files: list[Path] = []
    for name in REPORT_DIRS:
        folder = OUT / name
        if folder.exists():
            files.extend(p for p in folder.glob("*.md") if p.is_file())
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def _pick_report() -> Path | None:
    """推哪一份：当天有周报就推周报。

    同一天里，每日发现通常比周报更晚写（跑完整轮才收尾），按"最新"会挑到
    每日发现。但推送的重点是「这条追踪线走到哪了」，那三句只在周报里有。
    """
    reports = _newest(10)
    if not reports:
        return None
    today = datetime.now().date()
    weekly = [
        p
        for p in reports
        if p.parent.name == "追踪周报"
        and datetime.fromtimestamp(p.stat().st_mtime).date() == today
    ]
    return weekly[0] if weekly else reports[0]


def _section(text: str, title_kw: str) -> list[str]:
    """取某个二级标题下面的正文行，一直取到下一个二级标题。

    不能遇到空行就停：「## 跟前几周比，什么变了」下面先是 **新冒出来/在涨**
    这个加粗小标题，中间隔了空行，一停就把真正的内容全丢了。
    """
    out: list[str] = []
    hit = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            if hit:
                break
            hit = title_kw in s
            continue
        if hit and s:
            out.append(s)
    return out


def _clip(s: str, n: int) -> str:
    s = re.sub(r"[*`>]", "", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def digest(cfg: dict | None = None) -> tuple[str, str, str]:
    """凑一条手机上一眼能看完的摘要：(标题, 正文, 全文链接)。"""
    cfg = cfg or load_cfg()
    path = _pick_report()
    if path is None:
        return "AI 追踪 · 今天没有新报告", "产出目录还是空的，可能这次没跑起来。", ""

    text = path.read_text(encoding="utf-8", errors="replace")
    base = browse_base(cfg)
    link = f"{base}/?f={quote(path.relative_to(OUT).as_posix())}" if base else ""

    h1 = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("# ")), path.stem)
    # 手机通知栏只显示十几个字，标题得短。长名字留给正文。
    m = re.match(r"追踪\s*·\s*(.+?)\s*·\s*第\s*(\d+)\s*周", h1)
    if m:
        h1 = f"追踪第{m.group(2)}周 · {_clip(m.group(1), 18)}"
    else:
        h1 = _clip(h1, 34)

    lines: list[str] = []

    # 追踪周报：优先用「这条线走到哪了」那三句
    progress = _section(text, "这条线走到哪了")
    if progress:
        for item in progress[:3]:
            lines.append(_clip(re.sub(r"^\d+\.\s*", "", item), 120))
        moved = _section(text, "什么变了")
        bullets = [x for x in moved if x.startswith("-")]
        if bullets:
            lines.append("变化：" + _clip(bullets[0].lstrip("- "), 90))
    else:
        # 每日发现：第一句导读 + 前几条要点
        intro = [l.strip("_ ") for l in text.splitlines() if l.startswith("_逛了")]
        if intro:
            lines.append(_clip(intro[0], 100))
        bullets = [l for l in text.splitlines() if l.startswith("- ")]
        for item in bullets[:3]:
            lines.append("· " + _clip(item[2:], 100))

    if not lines:
        lines = [_clip(l, 100) for l in text.splitlines()[3:7] if l.strip()]

    body = "\n".join(lines)[:MAX_BODY]
    if link:
        body += f"\n\n全文 {link}"
    return h1, body, link


# ---------------------------------------------------------------- 通道

def _post(url: str, payload: dict, timeout: int = 12) -> tuple[bool, str]:
    import requests

    try:
        r = requests.post(url, json=payload, timeout=timeout)
        if r.status_code >= 300:
            return False, f"HTTP {r.status_code} {r.text[:80]}"
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"


def send_ntfy(cfg: dict, title: str, body: str, url: str) -> tuple[bool, str]:
    c = cfg.get("ntfy") or {}
    topic = (c.get("topic") or "").strip()
    if not topic:
        return False, "没填 topic"
    server = (c.get("server") or "https://ntfy.sh").rstrip("/")
    # 用 JSON 发布，而不是把标题塞进 HTTP 头：请求头只能用 latin-1，
    # 中文标题会直接抛 UnicodeEncodeError。
    payload = {
        "topic": topic,
        "title": title,
        "message": body,
        "tags": ["robot"],
        "priority": 3,
    }
    if url:
        payload["click"] = url
    ok, note = _post(server, payload)
    return ok, note if not ok else "ok"


def send_pushplus(cfg: dict, title: str, body: str, url: str) -> tuple[bool, str]:
    token = ((cfg.get("pushplus") or {}).get("token") or "").strip()
    if not token:
        return False, "没填 token"
    content = body + (f"\n\n<a href=\"{url}\">点开看全文</a>" if url else "")
    return _post(
        "https://www.pushplus.plus/send",
        {"token": token, "title": title, "content": content, "template": "html"},
    )


def send_serverchan(cfg: dict, title: str, body: str, url: str) -> tuple[bool, str]:
    key = ((cfg.get("serverchan") or {}).get("sendkey") or "").strip()
    if not key:
        return False, "没填 sendkey"
    desp = body + (f"\n\n[点开看全文]({url})" if url else "")
    return _post(f"https://sctapi.ftqq.com/{key}.send", {"title": title, "desp": desp})


def send_email(cfg: dict, title: str, body: str, url: str) -> tuple[bool, str]:
    c = cfg.get("email") or {}
    user, pwd, to = (c.get("user") or "").strip(), c.get("password") or "", (c.get("to") or "").strip()
    if not (user and pwd and to):
        return False, "邮箱配置不全"
    msg = MIMEText(body + (f"\n\n{url}" if url else ""), "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = user
    msg["To"] = to
    try:
        host, port = c.get("host") or "smtp.qq.com", int(c.get("port") or 465)
        with smtplib.SMTP_SSL(host, port, timeout=15) as s:
            s.login(user, pwd)
            s.sendmail(user, [to], msg.as_string())
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"


CHANNELS = {
    "ntfy": send_ntfy,
    "pushplus": send_pushplus,
    "serverchan": send_serverchan,
    "email": send_email,
}


def send(title: str, body: str, url: str = "", only: list[str] | None = None) -> list[tuple[str, bool, str]]:
    cfg = load_cfg()
    names = only or [n for n in cfg.get("启用", []) if n in CHANNELS]
    if not names:
        return [("无", False, "推送配置里没有启用任何通道")]
    out = []
    for name in names:
        ok, note = CHANNELS[name](cfg, title, body, url)
        out.append((name, ok, note))
    return out


# ---------------------------------------------------------------- 主

def main() -> None:
    ap = argparse.ArgumentParser(description="把最新报告推到手机")
    ap.add_argument("--test", action="store_true", help="发一条测试消息")
    ap.add_argument("--dry", action="store_true", help="只打印，不发送")
    ap.add_argument("--channel", default=None, help="临时指定通道，逗号分隔")
    args = ap.parse_args()

    cfg = load_cfg()
    if args.test:
        title = "追踪测试"
        body = (
            f"如果你在手机上看到这条，推送就是通的。\n触发时间 "
            f"{datetime.now():%m-%d %H:%M}"
        )
        url = f"{browse_base(cfg)}/" if browse_base(cfg) else ""
    else:
        title, body, url = digest(cfg)

    print(f"\n标题：{title}")
    print(f"正文：\n{body}\n")

    if args.dry:
        print("（--dry，没有真的发送）")
        return

    only = [x.strip() for x in args.channel.split(",")] if args.channel else None
    result = send(title, body, url, only)
    for name, ok, note in result:
        print(f"  {'✓' if ok else '✗'} {name}：{note}")

    if args.test and any(n == "ntfy" and ok for n, ok, _ in result):
        print(f"\n  手机上装 ntfy app，订阅话题：{cfg['ntfy']['topic']}")
    print()


if __name__ == "__main__":
    main()
