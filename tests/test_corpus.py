"""教材库补丁验收：中文断句 + PDF 页码映射 + 本地语料检索。

用法：
    D:\\anaconda3\\python.exe tests\\test_corpus.py

需要：ollama 里有 bge-m3；Windows 字体 simhei.ttf。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from deepdive.corpus.chunker import chunk_text, _split_sentences
from deepdive.corpus.indexer import CorpusIndex, extract_text_with_pages
from deepdive.search.local_corpus import LocalCorpusClient, LocalCorpusScraper

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------- T1 中文断句
ZH = (
    "导热是指物体各部分之间不发生相对位移时，依靠分子、原子及自由电子等微观粒子的热运动而产生的热能传递。"
    "傅里叶定律指出，单位时间内通过给定截面的导热量正比于垂直于该截面方向上的温度变化率。"
    "热导率是表征材料导热性能的物性参数，其数值取决于材料的种类、温度和结构。"
    "对于多层平壁，总热阻等于各层热阻之和，这是串联热阻叠加原则的直接应用。"
)
sents = _split_sentences(ZH)
check("T1 中文按句号切分", len(sents) == 4, f"切出 {len(sents)} 句（应为 4）")

# 引号收尾也要跟着断
ZH_Q = "老师说：“先算热阻。”然后我们代入温差除以热阻得到热流。"
check("T2 中文引号内句号不断错", len(_split_sentences(ZH_Q)) == 2,
      f"切出 {len(_split_sentences(ZH_Q))} 句（应为 2）")

# 英文不能被破坏
EN = "Heat conduction follows Fourier's law. The thermal resistance of a plane wall is L/k. "
check("T3 英文断句不受影响", len(_split_sentences(EN)) == 2,
      f"切出 {len(_split_sentences(EN))} 句（应为 2）")

# 小数点不能当句号
NUM = "热导率为 0.62 W/(m·K)，比热为 4.18 kJ/(kg·K)，二者之比为 1.5 左右。"
check("T4 小数点与数字不被误切", len(_split_sentences(NUM)) == 1,
      f"切出 {len(_split_sentences(NUM))} 句（应为 1）")


# ---------------------------------------------------------------- T5 分块不变式
long_zh = ZH * 60
chunks = chunk_text(long_zh, max_chars=1500, overlap_chars=200)
ok_invariant = all(long_zh[c.offset_start:c.offset_end] == c.text for c in chunks)
sizes = [len(c.text) for c in chunks]
check("T5 中文长文能切成多块", len(chunks) > 1, f"{len(chunks)} 块，最大 {max(sizes)} 字符")
check("T6 每块都等于原文切片", ok_invariant, "text == source[start:end]")
check("T7 没有超长块", max(sizes) <= 1500, f"最大块 {max(sizes)} 字符")


# ---------------------------------------------------------------- T8 无标点硬切
wall = "热" * 5000
hard = chunk_text(wall, max_chars=1500, overlap_chars=200)
check("T8 无标点长文被硬切", len(hard) >= 3 and max(len(c.text) for c in hard) <= 1500,
      f"{len(hard)} 块，最大 {max(len(c.text) for c in hard)} 字符")


# ---------------------------------------------------------------- T9 PDF 页边界
PAGE_TEXTS = [
    "第一章 绪论。本章介绍传热学的三种基本方式：导热、对流和辐射。"
    "导热是指物体各部分之间不发生相对位移时，依靠微观粒子的热运动而产生的热能传递。"
    "对流是指流体各部分之间发生相对位移、冷热流体相互掺混所引起的热量传递过程。"
    "辐射是指物体通过电磁波来传递能量的方式，不需要任何物质作为媒介。"
    "工程上常见的传热过程往往是几种方式同时起作用，称为复合换热。"
    "研究传热学的目的在于解决两类问题：强化传热与削弱传热。"
    "换热器设计中通常希望强化传热以减小设备体积，而建筑保温则希望削弱传热以降低能耗。",
    "第二章 稳态导热。傅里叶定律描述了热流密度与温度梯度的关系，是导热分析的基础。"
    "对于单层平壁，通过壁面的热流量等于温差除以热阻，热阻等于厚度除以热导率。"
    "对于多层平壁，总热阻等于各层热阻之和，这是串联热阻叠加原则的直接应用。"
    "多层平壁的层间温度可以通过分热阻计算，各层热流密度相等。"
    "圆筒壁的热阻表达式包含对数项，半径比越大热阻越大。"
    "热导率是表征材料导热性能的物性参数，金属的热导率通常远高于保温材料。"
    "接触热阻存在于两层材料的交界处，由表面粗糙度和接触压力决定。",
    "第三章 非稳态导热。集总参数法适用于毕渥数远小于零点一的情形。"
    "此时物体内部温度分布可以近似认为均匀，温度只随时间变化。"
    "时间常数反映物体温度响应的快慢，等于热容量与表面换热系数的比值。"
    "傅里叶数用于衡量非稳态过程中温度分布趋于均匀的程度。"
    "半无限大物体的非稳态导热可以用误差函数求解。"
    "建筑围护结构在室外温度周期性变化下的响应属于典型非稳态导热问题。",
]


def make_pdf(path: Path) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_font("hei", "", r"C:\Windows\Fonts\simhei.ttf")
    pdf.set_font("hei", size=12)
    for body in PAGE_TEXTS:
        pdf.add_page()
        pdf.multi_cell(0, 8, body)
    pdf.output(str(path))


tmpdir = Path(tempfile.mkdtemp(prefix="dd_corpus_"))
pdf_path = tmpdir / "传热学测试.pdf"
make_pdf(pdf_path)

text, bounds = extract_text_with_pages(pdf_path)
check("T9 PDF 抽出了 3 页边界", len(bounds) == 3, f"{len(bounds)} 页")


def _flat(s: str) -> str:
    import re as _re

    return _re.sub(r"\s+", "", s)


ok_pages = True
for i, (_, s, e) in enumerate(bounds):
    probe = _flat(PAGE_TEXTS[i][:24])
    if probe not in _flat(text[s:e]):
        ok_pages = False
check("T10 每页切片都能对上页码", ok_pages, "页文本与偏移一致")


# ---------------------------------------------------------------- T11 索引 + 检索 + 页码
async def main():
    db = tmpdir / "index.db"
    async with CorpusIndex(db, embed_model="bge-m3") as idx:
        added = await idx.index_file(pdf_path)
        hits = await idx.search("多层平壁的总热阻怎么计算", k=3)
        pages = [h.page for h in hits]
        check("T11 索引产出多个块", added > 1, f"{added} 块")
        check("T12 检索命中且带页码", bool(hits) and all(p is not None for p in hits),
              f"页码 {pages}")
        # 第二页讲多层平壁，top-1 应落在第 2 页
        check("T13 命中的页码正确", hits and hits[0].page == 2,
              f"top-1 页码 = {hits[0].page if hits else None}（应为 2）")

        client = LocalCorpusClient(idx)
        # 先把相关性门槛关掉，单测"能不能检索到"这件事本身
        import deepdive.search.local_corpus as lc

        lc.MIN_SIM = 0.0
        results_ = await client.search("集总参数法适用条件", max_results=2)
        check("T14 语料后端返回结果", len(results_) == 2,
              f"{len(results_)} 条，URL 示例 {results_[0].url if results_ else ''}"[:90])

        scraper = LocalCorpusScraper(idx, offline=True)
        page = await scraper.fetch(str(results_[0].url))
        check("T15 ScrapedPage 带出页码", page is not None and page.page is not None,
              f"page = {getattr(page, 'page', None)}")

        # 再打开门槛：库里没有这个主题时，必须返回空，而不是硬凑 top-k
        lc.MIN_SIM = 0.52
        none_hits = await client.search("量子计算机的退相干时间如何测量", max_results=2)
        check("T16 不相关查询被门槛挡住（返回空而不是硬凑）", len(none_hits) == 0,
              f"{len(none_hits)} 条" + (f"，最高相似度 {none_hits[0].distance:.3f}" if none_hits else ""))


asyncio.run(main())


# ---------------------------------------------------------------- 汇总
bad = [r for r in results if not r[1]]
print("\n" + "=" * 64)
print(f"总计 {len(results)} 项，通过 {len(results) - len(bad)} 项，失败 {len(bad)} 项")
if bad:
    print("失败项：" + "、".join(r[0] for r in bad))
    sys.exit(1)
print("全部通过 —— 中文分句与页码映射已生效。")
print(f"（测试文件保留在 {tmpdir}）")
