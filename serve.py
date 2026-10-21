"""在手机上翻报告的小网页。

为什么要有这个：推送只能塞三四句话，真要看清还得看全文。而全文是
Markdown，在手机上直接看 .md 是一堆符号。这里把 产出/ 目录挂成网页，
手机上点开就能读，顺便留一个"最新"入口当书签。

只在 Tailscale 网段里用，不对外网开放：整个服务监听 0.0.0.0，
但 100.x.x.x 这个地址只有你自己登录 Tailscale 的设备能访问。

    python serve.py              开服务，端口 8765
    python serve.py --port 9000  换端口
    python serve.py --open       顺便打开浏览器
"""

from __future__ import annotations

import argparse
import html
import socket
import sys
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = Path(__file__).resolve().parent
ROOT = WORK / "产出"

try:
    import markdown

    MD = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists"])
except Exception:
    MD = None

CSS = """
:root{--bg:#fbfbfa;--fg:#1c1c1e;--dim:#6b6b70;--line:#e3e3e0;--card:#fff;--accent:#2b6cb0}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--fg:#e6e6e8;--dim:#9a9aa2;--line:#2c2e33;--card:#1d1f23;--accent:#7db3e8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:18px 16px 64px}
a{color:var(--accent);text-decoration:none}
h1{font-size:1.4em;line-height:1.4;margin:.4em 0 .6em}
h2{font-size:1.15em;margin:1.8em 0 .6em;padding-bottom:.3em;border-bottom:1px solid var(--line)}
h3{font-size:1.02em;margin:1.4em 0 .4em}
ul,ol{padding-left:1.3em}
li{margin:.35em 0}
blockquote{margin:1em 0;padding:.55em .9em;border-left:3px solid var(--accent);
 background:var(--card);color:var(--dim);font-size:.95em;border-radius:0 6px 6px 0}
code{background:var(--card);border:1px solid var(--line);border-radius:4px;
 padding:.1em .35em;font-size:.88em}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;
 padding:12px;overflow-x:auto}
pre code{border:0;background:none;padding:0}
table{border-collapse:collapse;width:100%;display:block;overflow-x:auto;margin:1em 0;font-size:.94em}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:var(--card)}
hr{border:0;border-top:1px solid var(--line);margin:2em 0}
.meta{color:var(--dim);font-size:.85em;margin:.2em 0 1.4em}
.nav{display:flex;gap:14px;margin:0 0 18px;font-size:.9em;flex-wrap:wrap}
.nav a{color:var(--dim)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:12px 14px;margin:0 0 10px}
.card a{font-weight:600}
.card .sub{color:var(--dim);font-size:.82em;margin-top:3px}
.sec{margin-top:26px}
.empty{color:var(--dim);font-size:.9em}
"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body><div class=wrap>{body}</div></body></html>"
    ).encode("utf-8")


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _link(p: Path) -> str:
    return "/?f=" + quote(_rel(p))


def _fmt(p: Path) -> str:
    ts = datetime.fromtimestamp(p.stat().st_mtime)
    return ts.strftime("%m-%d %H:%M")


def _md_to_html(text: str) -> str:
    if MD is None:
        return "<pre>" + html.escape(text) + "</pre>"
    MD.reset()
    return MD.convert(text)


SECTIONS = ["追踪周报", "每日发现", "成果报告", "夜间记录", "问答记录"]


def resolve(rel: str) -> Path | None:
    """把请求里的相对路径落到 产出/ 里面。越界一律返回 None。

    这个服务监听 0.0.0.0，虽然只有 Tailscale 网段能连，但不做这层校验的话
    一个 `?f=../../Windows/win.ini` 就能把任意文件读出去。
    """
    root = ROOT.resolve()
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return None
    return target


def index_html() -> bytes:
    files = [p for p in ROOT.rglob("*.md") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    body = ["<h1>研究记录</h1>", f"<div class=meta>共 {len(files)} 份报告</div>"]

    if files:
        body.append("<h2>最新</h2>")
        for p in files[:3]:
            body.append(
                f"<div class=card><a href='{_link(p)}'>{html.escape(p.stem)}</a>"
                f"<div class=sub>{_fmt(p)}　{html.escape(p.parent.name)}</div></div>"
            )

    for name in SECTIONS:
        folder = ROOT / name
        if not folder.exists():
            continue
        items = sorted(
            [p for p in folder.glob("*.md") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        body.append(f"<div class=sec><h2>{html.escape(name)}（{len(items)}）</h2>")
        if not items:
            body.append("<div class=empty>还没有内容</div>")
        for p in items[:30]:
            body.append(
                f"<div class=card><a href='{_link(p)}'>{html.escape(p.stem)}</a>"
                f"<div class=sub>{_fmt(p)}</div></div>"
            )
        if len(items) > 30:
            body.append(f"<div class=empty>还有 {len(items) - 30} 份更早的，没列出来</div>")
        body.append("</div>")

    loose = [p for p in ROOT.glob("*.md") if p.is_file()]
    if loose:
        body.append("<div class=sec><h2>其他</h2>")
        for p in sorted(loose, key=lambda p: p.stat().st_mtime, reverse=True):
            body.append(f"<div class=card><a href='{_link(p)}'>{html.escape(p.stem)}</a></div>")
        body.append("</div>")

    return _page("研究记录", "".join(body))


class Handler(BaseHTTPRequestHandler):
    server_version = "AITrack/1.0"

    def log_message(self, fmt, *args):  # 别把访问日志刷进控制台
        pass

    def _send(self, code: int, payload: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _404(self, why: str = "找不到") -> None:
        self._send(404, _page("找不到", f"<h1>{html.escape(why)}</h1><p><a href='/'>回首页</a></p>"), "text/html; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return

        if parsed.path == "/" and "f" not in qs:
            self._send(200, index_html(), "text/html; charset=utf-8")
            return

        raw = qs["f"][0] if "f" in qs else unquote(parsed.path.lstrip("/"))
        target = resolve(raw)

        if target is None:
            self._404("这个路径不在产出目录里")
            return

        if not target.exists():
            self._404(f"没找到：{raw}")
            return
        if target.is_dir():
            self._send(200, index_html(), "text/html; charset=utf-8")
            return

        if target.suffix.lower() == ".md":
            text = target.read_text(encoding="utf-8", errors="replace")
            body = (
                "<div class=nav><a href='/'>← 全部</a>"
                f"<a href='/latest'>最新</a></div>"
                f"{_md_to_html(text)}"
                f"<div class=meta style='margin-top:2em'>{html.escape(_rel(target))}"
                f"　{_fmt(target)}</div>"
            )
            self._send(200, _page(target.stem, body), "text/html; charset=utf-8")
            return

        self._send(200, target.read_bytes(), "application/octet-stream")


def main() -> None:
    ap = argparse.ArgumentParser(description="手机上看报告的小网页")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    if not ROOT.exists():
        ROOT.mkdir(parents=True, exist_ok=True)

    import notify

    ip = notify._tailscale_ip()

    # 先探一下这个端口有没有人在服务。HTTPServer 默认 allow_reuse_address=1，
    # 在 Windows 上 SO_REUSEADDR 会让 bind「假成功」——起了一个进程但流量
    # 全走老那个，于是每次登录都多一个僵尸 python。探测比 bind 可靠。
    probe = "127.0.0.1" if args.bind in ("", "0.0.0.0", "::") else args.bind
    try:
        with socket.create_connection((probe, args.port), timeout=2):
            print()
            print(f"  {args.port} 端口已经有一个在服务了，这个实例直接退出。")
            print(f"  手机访问 http://{ip or probe}:{args.port} 不受影响。")
            print()
            return
    except OSError:
        pass  # 没人应答，正常起

    try:
        srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError:
        print()
        print(f"  {args.port} 端口被别的东西占了（不是本服务），起不来。")
        print("  换个端口：python serve.py --port 9000")
        print()
        return

    print("=" * 52)
    print("研究记录 · 手机网页")
    print("=" * 52)
    print(f"  本机      http://127.0.0.1:{args.port}")
    if ip:
        print(f"  Tailscale http://{ip}:{args.port}   ← 手机上用这个")
    print("  Ctrl+C 停止")
    print()

    if args.open:
        webbrowser.open(f"http://127.0.0.1:{args.port}")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
