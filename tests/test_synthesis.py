"""句子级合成校验验收。

用法：
    D:\\anaconda3\\python.exe tests\\test_synthesis.py
"""

import asyncio
import sys

from deepdive.analysis.synthesis import audit_body
from deepdive.models import Citation, Claim, ResearchReport
from deepdive.report.builder import ReportBuilder
from deepdive.report.markdown import report_to_markdown

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  -> {detail}" if detail else ""))


def cit(url, title, excerpt="", grounded=True):
    return Citation(url=url, title=title, excerpt=excerpt or None, grounded=grounded)


# 两个来源、三条 claim
c1 = Claim(
    text="视觉锚定奖励能拉高模型回复对图像信息的关联程度。",
    citations=[cit("https://a.example/1", "淘天提出视觉锚定奖励", "TPO 训练可以拉高模型回复对图像信息的关联程度")],
)
c2 = Claim(
    text="数据集中大约 12% 的数据偏好强度小于零。",
    citations=[cit("https://b.example/2", "深挖RLHF潜力", "约 12% 的偏好强度小于零")],
)
c3 = Claim(
    text="模型学会恒等 hack 后，50% 情况对外伪装对齐。",
    citations=[cit("https://c.example/3", "Anthropic最新实验显示奖励黑客危机", "50%情况对外伪装对齐")],
)
c4 = Claim(
    text="集总参数法适用于毕渥数远小于零点一的情形。",
    citations=[cit("https://d.example/4", "传热学", "集总参数法适用于毕渥数远小于零点一的情形")],
)
CLAIMS = [c1, c2, c3, c4]
ORDER = {
    "https://a.example/1": 1,
    "https://b.example/2": 2,
    "https://c.example/3": 3,
    "https://d.example/4": 4,
}


def run(text):
    return audit_body(text, CLAIMS, ORDER, heading="test")


# ---------------------------------------------------------------- 基础判定
body, drops = run("视觉锚定奖励能提升模型对图像信息的关注程度。[1]")
check("T1 有引用且无新事实 → 保留", len(drops) == 0 and body.strip() != "", f"删 {len(drops)} 句")

body, drops = run("视觉锚定奖励能提升模型对图像信息的关注程度。")
check("T2 无引用但不引入新事实 → 保留", len(drops) == 0 and body.strip() != "", f"删 {len(drops)} 句")

body, drops = run("OpenAI 的研究表明视觉锚定奖励有效。")
check("T2b 无引用且引入新事实（OpenAI）→ 删句",
      len(drops) == 1 and "OpenAI" in drops[0]["reason"],
      drops[0]["reason"] if drops else "")

body, drops = run("视觉锚定奖励能提升模型对图像信息的关注程度。[9]")
check("T3 引用号不存在 → 去掉错号，按无引用判定（保留）",
      len(drops) == 0 and "[9]" not in body and body.strip() != "",
      f"删 {len(drops)} 句，输出里{'仍有' if '[9]' in body else '已无'} [9]")

body, drops = run("OpenAI 的研究表明视觉锚定奖励有效。[9]")
check("T3b 引用号不存在且引入新事实 → 删句",
      len(drops) == 1 and "OpenAI" in drops[0]["reason"],
      drops[0]["reason"] if drops else "")

body, drops = run("TPO 训练能拉高模型回复对图像信息的关联程度。[1]")
check("T4 引文里出现过的缩写 → 保留", len(drops) == 0, f"删 {len(drops)} 句")

body, drops = run("TPO 的全称是 Truthful and Precise Output，它能提升模型对图像信息的关注。[1]")
check("T4b 缩写被脑补展开（Truthful / Precise / Output 不在引文里）→ 删句",
      len(drops) == 1 and "Truthful" in drops[0]["reason"],
      drops[0]["reason"] if drops else "")

body, drops = run("数据集中约 25% 的数据偏好强度小于零。[2]")
check("T5 数字与 claim 不符（25% vs 12%）→ 删句", len(drops) == 1 and "25" in drops[0]["reason"],
      drops[0]["reason"] if drops else "")

body, drops = run("数据集中约 12% 的数据偏好强度小于零。[2]")
check("T6 数字与 claim 一致 → 保留", len(drops) == 0, f"删 {len(drops)} 句")

body, drops = run("集总参数法适用于毕渥数远小于 0.1 的情形。[4]")
check("T6b 零点一 与 0.1 视为同一个数 → 保留", len(drops) == 0,
      f"删 {len(drops)} 句" + (f"（{drops[0]['reason']}）" if drops else ""))

body, drops = run("集总参数法适用于 Biot 数远小于 0.1 的情形。[4]")
check("T6c 毕渥数 与 Biot 视为同一术语 → 保留", len(drops) == 0,
      f"删 {len(drops)} 句" + (f"（{drops[0]['reason']}）" if drops else ""))

body, drops = run("Reynolds 数远小于 0.1 时可忽略惯性力。[4]")
check("T6d 术语对照不外溢（引用毕渥的 claim 提到 Reynolds）→ 删句",
      len(drops) == 1 and "Reynolds" in drops[0]["reason"],
      drops[0]["reason"] if drops else "")

body, drops = run("In the experiment, Anthropic found that models sabotaged the repo [3].")
check("T7 来源标题里出现的主体名 → 允许", len(drops) == 0, drops[0]["reason"] if drops else "")

body, drops = run("In the experiment, OpenAI found that models sabotaged the repo [3].")
check("T8 把 A 来源的结论归给 B（OpenAI）→ 删句", len(drops) == 1 and "OpenAI" in drops[0]["reason"],
      drops[0]["reason"] if drops else "")


# ---------------------------------------------------------------- 段落结构
mixed = "视觉锚定奖励能提升模型对图像信息的关注程度。[1]\n\nOpenAI 认为这条非常关键。\n\n数据集中约 12% 的偏好强度小于零。[2]"
body, drops = run(mixed)
has_break = "\n\n" in body
check("T9 删句后段落分隔保留", has_break, f"删 {len(drops)} 句，段落分隔{'在' if has_break else '丢了'}")

kept_sentences = body.count("。") + body.count(".")
check("T10 只删该删的那一句", len(drops) == 1, f"保留 {kept_sentences} 句，删 1 句")


# ---------------------------------------------------------------- 与 ReportBuilder 集成
BODY_WITH_ONE_BAD_SENTENCE = (
    "视觉锚定奖励能提升模型对图像信息的关注程度。[1]"
    "TPO 的全称是 Truthful and Precise Output。[1]"
    "数据集中约 12% 的偏好强度小于零。[2]"
)


class _Stub:
    async def complete(self, prompt, temperature=0.0):
        class _R:
            content = BODY_WITH_ONE_BAD_SENTENCE
            cost_usd = 0.0

        return _R()


report = asyncio.run(ReportBuilder(llm=_Stub()).build("Q", CLAIMS))
sec = report.sections[0]
check(
    "T11 Builder 出口已过滤：正文不含脑补句",
    "Truthful and Precise Output" not in sec.body,
    f"节内剩 {sec.body.count('。')} 句，记录删 {sec.dropped} 句",
)
check("T12 dropped 计数被记录", report.sentences_dropped >= 4, f"共删 {report.sentences_dropped} 句")

md = report_to_markdown(report)
check("T13 页脚披露删了多少句", "句子校验删去" in md, "页脚含审核说明")


# ---------------------------------------------------------------- 汇总
bad = [r for r in results if not r[1]]
print("\n" + "=" * 64)
print(f"总计 {len(results)} 项，通过 {len(results) - len(bad)} 项，失败 {len(bad)} 项")
if bad:
    print("失败项：" + "、".join(r[0] for r in bad))
    sys.exit(1)
print("全部通过 —— 句子级合成校验已生效。")
