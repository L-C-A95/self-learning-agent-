import os
import re

# 黑名单：只要报告里出现了这些字眼，说明抓到了反爬虫页面或垃圾网站
BLACKLIST = [
    "当前环境异常", "完成验证后即可继续访问", "去验证",
    "轻点两下取消赞", "扫一扫",
    "wappoc_appmsg", "captcha", "Just a moment", "Enable JavaScript",
]

MIN_CHARS = 600          # 修好 grounding 后报告会变短，原来的 1000 太严
MIN_CHARS_LOCAL = 300    # 教材/论文库回答通常更短更集中
MAX_DUP_RATIO = 0.05     # Evidence 重复率上限

# 本地语料来源长这样：`E:\...\传热学.pdf` 或 http://localhost/corpus/...
# 这类报告"只有 1 个来源"是正常的 —— 一本书就是一个来源，不该套用
# 网页检索的"至少 3 个独立来源"规则。
_LOCAL_SOURCE = re.compile(r"`[^`]*\.(pdf|md|txt|html)`|localhost/corpus", re.I)


def _split_sections(content: str) -> dict[str, str]:
    """按二级标题切分，返回 {标题: 正文}。"""
    parts: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for line in content.splitlines():
        if line.startswith("## "):
            if cur is not None:
                parts[cur] = "\n".join(buf)
            cur = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if cur is not None:
        parts[cur] = "\n".join(buf)
    return parts


def check_report_quality(report_path):
    """
    质检报告质量。
    返回 (True, "通过原因") 或 (False, "拒绝原因")
    """
    if not os.path.exists(report_path):
        return False, "报告文件不存在"

    with open(report_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. 反爬黑名单
    for word in BLACKLIST:
        if word in content:
            return False, f"拦截：发现反爬/验证码页面特征词 '{word}'"

    # 2. 模型自称提取不到有效信息
    if "No verifiable claims could be extracted" in content:
        return False, "拦截：模型声明无法提取有效信息"

    # 3. 只统计 Sources 章节内的条目（原实现会把正文任意"数字+点"都数进去）
    sections = _split_sections(content)
    sources_block = sections.get("Sources", "")
    sources_count = len(re.findall(r"^\s*\d+\.\s", sources_block, re.MULTILINE))

    # 本地库模式：来源是硬盘上的 PDF，一本就是一源，门槛降到 1
    local_mode = bool(_LOCAL_SOURCE.search(sources_block)) and sources_count >= 1
    min_sources = 1 if local_mode else 3
    min_chars = MIN_CHARS_LOCAL if local_mode else MIN_CHARS

    if sources_count < min_sources:
        return False, (
            f"拦截：有效来源不足（只有 {sources_count} 个，要求至少 {min_sources} 个）"
        )

    # 4. 引用越界：正文里的 [n] 必须落在来源编号范围内
    body = content.split("## Sources")[0]
    refs = [int(n) for n in re.findall(r"\[(\d{1,3})\]", body)]
    if refs and max(refs) > sources_count:
        return False, (
            f"拦截：正文引用到 [{max(refs)}]，但只有 {sources_count} 个来源（悬空引用）"
        )

    # 5. Evidence 重复率（3 倍膨胀会直接被这条抓住）
    evidence_block = sections.get("Evidence", "")
    items = re.findall(r"^\*\*\d+\.\*\*\s+(.+)$", evidence_block, re.MULTILINE)
    if items:
        keys = [" ".join(i.lower().split()) for i in items]
        dup_ratio = 1 - len(set(keys)) / len(keys)
        if dup_ratio > MAX_DUP_RATIO:
            return False, (
                f"拦截：Evidence 重复率 {dup_ratio:.0%}"
                f"（{len(keys)} 条里只有 {len(set(keys))} 条唯一，上限 {MAX_DUP_RATIO:.0%}）"
            )

    # 6. 正文太短
    if len(content) < min_chars:
        return False, f"拦截：正文太短（仅 {len(content)} 字符，要求 {min_chars}）"

    return True, (
        f"通过：{sources_count} 个来源"
        f"{'（本地库）' if local_mode else ''}，"
        f"{len(set(i.lower() for i in items)) if items else 0} 条唯一证据，"
        f"{len(content)} 字符"
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python qc_gate.py <报告路径>")
        sys.exit(1)

    ok, msg = check_report_quality(sys.argv[1])
    print(("通过 " if ok else "拒绝 ") + msg)
