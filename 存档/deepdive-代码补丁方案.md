# DeepDive 代码诊断与补丁方案

诊断对象
- 你的代码（4 个文件）：`auto_research.py`、`autonomous_loop.py`、`qc_gate.py`、`vector_kb.py`
- 真正的引擎：`D:\anaconda3\Lib\site-packages\deepdive\`（**pip 包 deepdive_research 0.3.2，纯 Python，可读可改，3397 行**）
- 检索：自建 SearXNG（`settings.yml`，引擎只开了 baidu / bing / 360search / sogou）

**重要结论：你之前问的"反爬页、3 倍膨胀、引用悬空、跑题"，全部不在你的 4 个文件里，也不在模型里——在 deepdive 包的源码里，一共 5 个确切位置。**

---

## 一、五个根因，逐个定位到行

### 根因 1　3 倍膨胀：`report/builder.py:73`

```python
for heading in ("Background", "Key findings", "Contradictions & open questions"):
    r = await self.llm.complete(...)
    sections.append(ReportSection(heading=heading, body=r.content, claims=claims))
                                                              # ↑ 同一个 claims 列表
```

三个章节，每个都塞进**同一份完整的 claims 列表**。然后 `report/markdown.py:29` 把它们拍平：

```python
all_claims = [c for s in report.sections for c in s.claims]
```

**3 个章节 × N 条 = 3N 条。** 永远是 3 倍，因为章节数恒为 3。

这解释了你五份报告的全部数字：30→90、27→81、53→159、8→24、31→93。**不是数据问题，是渲染时数了三遍。**

### 根因 2　引用号悬空：`report/builder.py:55`

```python
numbered = [f"[{i + 1}] {c.text}" for i, c in enumerate(claims)]
```

`[n]` 里的 n 是 **claim 序号**（最大 53），但 Sources 列表是按 **source 序号**（最大 5）渲染的（`markdown.py:24`）。

模型被告知"用 [n] 引用上面的编号 claims"，于是写了 `[30]`——而来源只有 4 条。

**两套编号体系，从来没有对齐过。** 这就是 26 个悬空引用的来源。

### 根因 3　grounding 校验方向反了：`analysis/grounding.py:144`

```python
def ground_citation(body: str, citation: Citation) -> Citation:
    if not citation.excerpt:
        return citation.model_copy(update={"grounded": False})
    offsets = find_excerpt_offsets(body, citation.excerpt)   # 只查 excerpt 在不在 body
    ...
    "grounded": True,
```

它只验证 **摘录是否出现在网页里**，从不检查 **claim.text 是否被 excerpt 支持**。

引文本来就是从网页复制的，所以这个检查永远成立 → `Chain.grounded`（`models.py:48`，只要任一 citation 成立即为真）→ 永远 True。

**这就是"93/93 grounded"的真相：它是个恒真命题。**

于是 `AlphaGo` 被写成 `AlphaGo Zero`、`Policy Gradient` 被加上"蒙特卡洛"、六字小标题被写成"MDP 是核心概念"——全部通过。

### 根因 4　搜的是百度系引擎：`settings.yml`

```yaml
engines:
  - baidu (enabled) - bing (enabled) - 360search (enabled) - sogou (enabled)
  - google (disabled) - duckduckgo (disabled) - brave (disabled) - wikipedia (disabled)
```

`baidu + 360 + sogou` 是 CSDN、道客巴巴、词典站的重灾区。你的 31 条声明里 60% 来自这些站，不是模型的错。

**而且 DeepDive 内置了来源过滤（`search/filters.py`），CLI 也有 `--allow-domains / --block-domains`，你的 `auto_research.py` 一次都没用。**

### 根因 5　章节标题重复：`report/markdown.py:19`

```python
lines.append(f"## {section.heading}\n")
lines.append(section.body.strip() + "\n")
```

模型有时会在 body 开头自己再写一遍 `## Key findings`，于是渲染成两个标题。这就是你第一份报告 13/15 行、23/25 行重复的原因。

---

## 二、一处自我纠正

我前面说"cost $0.0000 说明成本统计没接上"——**这句是错的**。

`builder.py:64` 累加的是 `result.cost_usd`，而本地 ollama 推理不产生 API 费用，所以 0.0000 是正确值。**不是 bug，是我判断错了。**

---

## 三、六个补丁（精确到字符串替换）

### P1　Evidence 去重（修根因 1）

`report/markdown.py`
```python
# 原
all_claims = [c for s in report.sections for c in s.claims]

# 改
all_claims = []
_seen_claim_keys: set[str] = set()
for _section in report.sections:
    for _claim in _section.claims:
        _key = " ".join(_claim.text.lower().split())
        if _key in _seen_claim_keys:
            continue
        _seen_claim_keys.add(_key)
        all_claims.append(_claim)
```

### P2　引用编号改用来源序号（修根因 2）

`report/builder.py`，在 `build()` 里把 claim 编号替换为来源编号，并让 sources 用同一套顺序：

```python
# 原
numbered = [f"[{i + 1}] {c.text}" for i, c in enumerate(claims)]
claims_block = "\n".join(numbered)

# 改
source_order: dict[str, int] = {}
for _c in claims:
    for _cit in _c.citations:
        source_order.setdefault(str(_cit.url), len(source_order) + 1)

def _primary(c):
    for cit in c.citations:
        if cit.grounded:
            return str(cit.url)
    return str(c.citations[0].url) if c.citations else ""

numbered = [
    f"[{source_order[_primary(c)]}] {c.text}"
    for c in claims
    if c.citations and _primary(c) in source_order
]
claims_block = "\n".join(numbered)
```

同时 `sources = self._collect_sources(claims)` → `sources = self._collect_sources(claims, source_order)`，并把方法改为：

```python
@staticmethod
def _collect_sources(claims, order=None):
    seen: dict[str, Citation] = {}
    for claim in claims:
        for c in claim.citations:
            seen.setdefault(str(c.url), c)
    items = list(seen.items())
    if order:
        items.sort(key=lambda kv: order.get(kv[0], 10**6))
    return [c for _, c in items]
```

**验收：`max(正文引用号) == len(sources)`，不再有悬空。**

### P3　grounding 加语义闸门（修根因 3，最关键）

新建 `analysis/support.py`：

```python
from __future__ import annotations
import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def is_cjk(s: str) -> bool:
    return bool(_CJK.search(s or ""))


def entities(s: str) -> set[str]:
    out = set(_NUM.findall(s or ""))
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-]{1,}", s or ""):
        tok = m.group(0)
        if tok.isupper() or any(ch.isupper() for ch in tok[1:]):
            out.add(tok)
    return out


def check_support(claim_text: str, excerpt: str, *, crosslingual_ok: bool = False):
    """claim 的信息是否都在 excerpt 里。跨语言一律不放行——字符串都比不了，凭什么说 grounded。"""
    if not claim_text or not excerpt:
        return False, "empty"
    if is_cjk(claim_text) != is_cjk(excerpt):
        if crosslingual_ok:
            return True, "crosslingual-allowed"
        return False, "claim 与引文语言不一致，无法逐字校验"
    ex = excerpt.casefold()
    missing = sorted(e for e in entities(claim_text) if e.casefold() not in ex)
    if missing:
        return False, "claim 含引文中不存在的信息：" + ", ".join(missing[:5])
    return True, "ok"
```

在 `analysis/claims.py` 的 `_extract_grounded` 末尾接上：

```python
# 原
if grounded.grounded:
    grounded.confidence = 0.8
out.append(grounded)

# 改
if grounded.grounded:
    _ok, _why = check_support(
        grounded.text, (grounded.citations[0].excerpt or "")
    )
    if _ok:
        grounded.confidence = 0.8
    else:
        log.info("claim_support_rejected", url=str(page.url),
                 reason=_why, claim=grounded.text[:80])
        grounded = grounded.model_copy(update={
            "citations": [c.model_copy(update={"grounded": False})
                          for c in grounded.citations],
            "confidence": 0.4,
        })
out.append(grounded)
```

顶部加 `from deepdive.analysis.support import check_support`。

**用你那份 qwen 报告回归测试，这一条应该能抓出：**
- `AlphaGo Zero`（引文写的是 AlphaGo）
- `蒙特卡洛`（引文没有）
- `MDP`（引文只有六字标题）
- 全部中文 claim 配英文引文（语言不一致）

### P4　来源过滤支持路径（修根因 4）

`search/filters.py`，把 block 判断改为支持含 `/` 的路径条目：

```python
# 原
if self.block and any(_match(host, entry) for entry in self.block):
    return False

# 改
if self.block:
    low = url.lower()
    for entry in self.block:
        if "/" in entry:
            if entry.lower().lstrip("/") in low:
                return False
        elif _match(host, entry):
            return False
```

然后 `auto_research.py` 的 cmd 里加：

```python
BLOCK = "blog.csdn.net,dict.cn,doc88.com,wenku.baidu.com,arxiv.org/search,mp.weixin.qq.com"
cmd = [DEEPDIVE_CMD, "research", topic, "-m", MODEL, "-b", SEARCH_BACKEND,
       "-o", report_file, "--force", "--block-domains", BLOCK]
```

（`mp.weixin.qq.com` 是你 4 次反爬灾难的唯一来源，建议直接封。要留就删掉这段。）

另外改 `settings.yml`，把引擎换成学术向：

```yaml
engines:
  - name: arxiv
    disabled: false
  - name: crossref
    disabled: false
  - name: semantic scholar
    disabled: false
  - name: bing
    disabled: false
  - name: google
    disabled: false
  # 关掉 baidu / 360search / sogou
```

### P5　去掉重复标题（修根因 5）

`report/markdown.py` 顶部加 `import re`，然后：

```python
# 原
lines.append(f"## {section.heading}\n")
lines.append(section.body.strip() + "\n")

# 改
lines.append(f"## {section.heading}\n")
_body = section.body.strip()
_body = re.sub(
    r"^#{1,6}\s*" + re.escape(section.heading) + r"\s*\n?",
    "", _body, count=1, flags=re.IGNORECASE,
).strip()
lines.append(_body + "\n")
```

### P6　质检闸门挪到结构化层（改你的 `qc_gate.py`）

现在它读的是渲染后的 Markdown，只能靠关键词猜；而且 `re.findall(r'^\s*\d+\.', ...)` 会把正文里任何"数字+点"的行都数成来源。

两个改法：
1. **短平快**：给 `check_report_quality` 加三条硬检查——Evidence 区重复率 < 5%、`max(引用号) <= 来源数`、中文报告里不得出现超过 10% 的英文 claim。
2. **正道**：让 `deepdive.exe` 输出 JSON（`--format json` 如果有），直接对 `claims[].citations[].grounded` 做统计，不再解析 Markdown。

---

## 四、执行顺序（每一步都能单独验收）

| 步 | 动作 | 验收标准 |
|---|---|---|
| 1 | 备份 `deepdive` 包：`cp -r site-packages/deepdive site-packages/deepdive.orig` | 目录存在 |
| 2 | P1 + P5 | 重跑同一问题，Evidence 条数 = 唯一声明数，标题不再重复 |
| 3 | P2 | `max(引用号) == len(sources)` |
| 4 | P3 | 那 4 条篡改案例全部被拒，报告可能出现"无法提取有效声明"——**这是对的** |
| 5 | P4 + settings.yml | 来源里不再出现 CSDN / 词典 / 搜索结果页 |
| 6 | 改 `qc_gate.py` | 七份旧报告重跑闸门，应该拦下 5 份、放行 2 份 |

**关键提醒：第 4 步之后报告会变短甚至变空，这不是退步。** 你现在的"丰富"是 3 倍重复 + 未经校验的 claim 撑出来的。修完之后条目会少，但每一条都真。

---

## 五、需要你定的三件事

1. **改包还是外挂？**
   - 改包（推荐）：直接改 site-packages，立即生效，先备份 `.orig`。缺点：`pip install -U` 会覆盖，需要留一份补丁记录。
   - 外挂：不动包，只在 `auto_research.py` 里做后处理。缺点：P3（grounding）改不了，因为 claim 上下文在包里。

2. **要不要封 `mp.weixin.qq.com`？** 它是你 4 次反爬灾难的唯一来源，但也是你唯一的英文以外信息源。

3. **报告要单语还是双语？** 中文提问 → 中文报告（P3 里 `crosslingual_ok=False` 会强制这个）。现在你是中文标题配英文正文，这条规则一旦开启会自动统一。

---

## 六、另两个文件：你的"自我学习"闭环已经在跑了，而且在原地转圈

`autonomous_loop.py` 的结构是对的：读知识库 → 让 7B 提出下一个问题 → 去重 → 调 `auto_research.py` → 入库。**那 6 份问题（反事实 DPO、IFCD 多对象…）就是这个循环自己生成的。** 但它有三个具体问题：

**1. 去重形同虚设**
```python
if any(new_question in t or t in new_question for t in topics):
```
`topics` 是整份报告的主题词，拿一个完整疑问句去和主题词比子串，几乎永不命中。

证据：`report_20261017_165415`（08:54）和 `report_20261017_172448`（09:26）**是同一个问题的中英文两版**——"IFCD方法在多对象视觉场景下是否会失效？" / "Does IFCD method fail in high-density multi-object visual scenes?"。它 32 分钟后把自己问过的问题又问了一遍。

改法：用你已经有的 `bge-m3` 把问题向量化，与库中所有 question 比余弦，**> 0.85 判重**。顺便把"是否会失效"这类句式记成问题模板，同一个模板最多用 2 次。

**2. 它是从自己的旧产出里选题的**
```python
sample_texts = random.sample(metadata, min(3, len(metadata)))
```
随机抽 3 段当"研究笔记"。而库里存的正是它自己写的报告正文——其中包含"当前环境异常"那一批。**这就是我之前说的噪声自我繁殖，你已经把它实现出来了。**

改法：选题前先过滤——只从 `score` 高且来源可信的段落里抽；或者干脆改成"从待读清单里选题"（你手上的教材章节、真题错题），而不是从旧报告里。

**3. `vector_kb.py` 存的东西，对考研没用**

它索引进库的是**报告正文**（AI 写的），不是**原文**。对"验证一个说法有没有依据"这个用途来说，AI 写的正文恰恰是最不该当证据的东西。

但这个文件的骨架（bge-m3 向量 + cosine top-k）**完全可以复用到教材库**，只需改两处：
- 数据源：从 `report_*.md` 换成教材 PDF 切出来的"节"
- 元数据：`{"book": "传热学", "edition": 5, "chapter": "2", "section": "2-3", "page": 87, "text": "原文…"}`

这样 `search()` 返回的就是**原句 + 页码**，而不是 AI 的转述。你要的"教材库"，本质上就是把 `vector_kb.py` 的 `add_report` 换成 `add_textbook`，其余不用动。

---

## 七、最后一句

你原来的 4 个文件其实没写错。`qc_gate.py` 的逻辑是对的、位置是错的——它在**终稿**上查关键词，而病在**中间产物**里。`autonomous_loop.py` 的思路也是对的，只是去重太松、选题的池子脏。

真正需要改的是那 6 处补丁 + 2 处闭环调整，加起来不到 100 行。**其中 P3（grounding 语义闸门）一项，就能让你那五份报告的 12% 命中率变成"要么真、要么明说没有"。**
