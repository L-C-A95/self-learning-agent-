"""补丁验收脚本：验证 P1-P5 是否真的生效。

用法：
    D:\\anaconda3\\python.exe tests\\test_patches.py
"""

import asyncio
import re
import sys

from deepdive.analysis.support import check_support
from deepdive.models import Citation, Claim, ReportSection, ResearchReport
from deepdive.report.builder import ReportBuilder
from deepdive.report.markdown import report_to_markdown
from deepdive.search.filters import SourceFilter

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  -> {detail}" if detail else ""))


def cit(url, title, excerpt, grounded=True):
    return Citation(
        url=url, title=title, excerpt=excerpt, grounded=grounded,
        offset_start=0, offset_end=len(excerpt or ""),
    )


# ---------------------------------------------------------------- T1 Evidence 去重
claims = [
    Claim(text="Claim A about thermal resistance.", citations=[cit("https://a.example/1", "A", "ex")]),
    Claim(text="Claim B about Nusselt number.", citations=[cit("https://b.example/2", "B", "ex")]),
]
report = ResearchReport(
    question="Q",
    summary="S",
    sections=[ReportSection(heading=h, body="paragraph", claims=claims)
              for h in ("Background", "Key findings", "Contradictions & open questions")],
    sources=[c.citations[0] for c in claims],
)
md = report_to_markdown(report)
evidence_items = len(re.findall(r"^\*\*\d+\.\*\* ", md, re.MULTILINE))
check("T1 三章节不再产生 3 倍重复", evidence_items == 2, f"Evidence 条数 = {evidence_items}（应为 2）")


# ---------------------------------------------------------------- T2 重复标题
report2 = ResearchReport(
    question="Q",
    summary="S",
    sections=[ReportSection(heading="Key findings", body="## Key findings\n\n正文内容", claims=[])],
    sources=[],
)
md2 = report_to_markdown(report2)
heading_count = md2.count("## Key findings")
check("T2 章节标题不再重复", heading_count == 1, f"'## Key findings' 出现 {heading_count} 次（应为 1）")


# ---------------------------------------------------------------- T3 引用编号对齐
class _StubLLM:
    def __init__(self):
        self.prompts = []

    async def complete(self, prompt, temperature=0.0):
        self.prompts.append(prompt)

        class _R:
            content = "正文 [1] 与 [2]"
            cost_usd = 0.0

        return _R()


src_a = cit("https://a.example/1", "SourceA", "ex")
src_b = cit("https://b.example/2", "SourceB", "ex")
claims3 = [
    Claim(text="c1", citations=[src_a]),
    Claim(text="c2", citations=[src_b]),
    Claim(text="c3", citations=[src_a]),
]
stub = _StubLLM()
built = asyncio.run(ReportBuilder(llm=stub).build("Q", claims3))
refs = [int(n) for n in re.findall(r"\[(\d+)\]", stub.prompts[1])]
check(
    "T3 正文引用号 = 来源序号且不越界",
    max(refs) <= len(built.sources) and sorted(set(refs)) == [1, 2],
    f"引用号 {sorted(set(refs))}，来源数 {len(built.sources)}",
)

# 同一文档的多个 chunk（#offset 不同）应合并成一个来源，且链接不带 fragment
src_c1 = cit("http://localhost/corpus/book.pdf#offset=0", "book.pdf", "ex")
src_c2 = cit("http://localhost/corpus/book.pdf#offset=1500", "book.pdf", "ex")
built2 = asyncio.run(
    ReportBuilder(llm=_StubLLM()).build(
        "Q",
        [Claim(text="a", citations=[src_c1]), Claim(text="b", citations=[src_c2])],
    )
)
check(
    "T3b 同一文档多 chunk 合并为一个来源",
    len(built2.sources) == 1 and "#" not in str(built2.sources[0].url),
    f"来源 {len(built2.sources)} 个，url={built2.sources[0].url}",
)


# ---------------------------------------------------------------- T4 语义支撑校验
cases = [
    ("AlphaGo Zero通过行为克隆初始化策略网络。", "AlphaGo Initialize Policy Network by Behavior Cloning", False),
    ("AlphaGo Zero使用蒙特卡洛政策梯度进行策略网络训练。", "Train Policy Network Using Policy Gradient", False),
    ("强化学习中的马尔可夫决策过程（MDP）是其核心概念之一。", "马尔可夫决策过程", False),
    ("AlphaGo Zero was initialized by behavior cloning.", "AlphaGo Initialize Policy Network by Behavior Cloning", False),
    ("The FIRM-Edit-8B model reflects human criteria.", "train specialized reward models (FIRM-Edit-8B and FIRM-Gen-8B)", True),
    ("传热学中努塞尔数用于无量纲化对流换热系数。", "努塞尔数 Nu 是无量纲的对流换热系数。", True),
]
for text, excerpt, expected in cases:
    ok, why = check_support(text, excerpt)
    check(f"T4 支撑校验 「{text[:22]}…」", ok == expected,
          f"判定={'通过' if ok else '拒绝'} / {why}")


# ---------------------------------------------------------------- T5 来源路径过滤
f = SourceFilter(block=("blog.csdn.net", "arxiv.org/search", "wappoc_appmsg"))
cases5 = [
    ("https://blog.csdn.net/x/article/1", False),
    ("https://arxiv.org/abs/2503.03746", True),
    ("https://arxiv.org/search/cs?query=x", False),
    ("https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc=1", False),
    ("https://mp.weixin.qq.com/s/real-article", True),
]
for url, expected in cases5:
    kept = f.keep(url)
    check(f"T5 过滤 「{url[:46]}」", kept == expected,
          f"{'放行' if kept else '拦截'}（应{'放行' if expected else '拦截'}）")


# ---------------------------------------------------------------- T6 抽取链路接线
from deepdive.analysis.claims import ClaimExtractor
from deepdive.analysis.grounding import _GroundedClaim, _GroundedClaimList
from deepdive.models import ScrapedPage

PAGE_TEXT = (
    "The FIRM-Edit-8B model reflects human criteria. "
    "AlphaGo Initialize Policy Network by Behavior Cloning."
)


class _ExtractStub:
    async def extract(self, prompt, schema, temperature=0.2):
        return _GroundedClaimList(
            claims=[
                _GroundedClaim(
                    text="The FIRM-Edit-8B model reflects human criteria.",
                    excerpt="The FIRM-Edit-8B model reflects human criteria.",
                ),
                _GroundedClaim(
                    text="AlphaGo Zero was initialized by behavior cloning.",
                    excerpt="AlphaGo Initialize Policy Network by Behavior Cloning.",
                ),
            ]
        )


page = ScrapedPage(url="https://example.com/p", title="p", text=PAGE_TEXT)
extracted = asyncio.run(ClaimExtractor(llm=_ExtractStub(), ground=True).extract(page))
flags = [(c.text[:30], c.grounded) for c in extracted]
kept = [f for f in flags if f[1]]
dropped = [f for f in flags if not f[1]]
check(
    "T6 抽取链路：真声明保留、篡改主语的被拒",
    len(kept) == 1 and len(dropped) == 1,
    f"保留 {len(kept)} 条，拒绝 {len(dropped)} 条",
)


# ---------------------------------------------------------------- 汇总
bad = [r for r in results if not r[1]]
print("\n" + "=" * 62)
print(f"总计 {len(results)} 项，通过 {len(results) - len(bad)} 项，失败 {len(bad)} 项")
if bad:
    print("失败项：" + "、".join(r[0] for r in bad))
    sys.exit(1)
print("全部通过 —— 补丁已生效。")
