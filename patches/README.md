# DeepDive 补丁记录（2026-10-18）

## 改了什么

安装位置：`D:\anaconda3\Lib\site-packages\deepdive\`（pip 包 `deepdive_research 0.3.2`）

| 文件 | 改动 | 对应问题 |
|---|---|---|
| `search/local_corpus.py` | **新增相关性门槛**（余弦 ≥ 0.52，env `DEEPDIVE_MIN_SIM`）+`precheck()` 用**原始问题**判定库内是否有相关内容 | 库里没相关内容时硬凑 top-k、拿垃圾当答案 |
| `corpus/indexer.py` | `search()` 返回 `similarity` 并支持 `min_similarity` 过滤；新增 pages 表、按页切块、记录 embedding 模型 | 同上、拿不到页码 |
| `pipeline.py` | 检索前先对原始问题做相关性预检，不过则整轮不作答 | 改写后的查询把跑题问题"洗白" |
| `trace/recorder.py` | `RecordingSearch.__getattr__` 转发到真实后端 | CLI 只要带 `-o` 就自动开 trace，包装后 `precheck` 对管线不可见，门槛被静默绕过 |
| `report/markdown.py` | 语料来源改渲染为**真实本地路径+页码**（原 `http://localhost/corpus/...` 点开 404）；Evidence 去重；标题剥离；页脚披露删句数 | 链接 404、3 倍膨胀、标题重复 |
| `report/builder.py` | 正文语言跟随提问语言；`[n]` 用来源序号；摘要与三章都过合成校验 | 中文提问得到英文报告、引用悬空 |
| `analysis/support.py` | claim↔excerpt 校验、中英术语对照、`零点一`↔`0.1`、表格碎片识别 | grounded 恒真、术语误判 |
| `analysis/claims.py` | 接入 support 校验与表格碎片过滤；citation 按 (文档, 摘录) 去重 | 表格行被当结论、同一摘录重复 |
| `analysis/synthesis.py` | **新增**句子级合成校验 | 缩写脑补、跨来源归因 |
| `corpus/chunker.py` | 中文断句、长句硬切、overlap 不超预算 | 中文教材整本塌成 1 块 |
| `search/filters.py` | block 支持 URL 子串（路径级） | 反爬页、搜索结果页 |
| `models.py` | `page` / `dropped` / `sentences_dropped` 字段 | 页码与审核计数透传 |

## 2026-10-18 晚：换模型 + 规则库（方向 C）

### 1. `actants/llm/ollama.py`（新，包外）

qwen3 在 Ollama 里**默认开启 thinking**，思考链走 `message.thinking` 不进 `content`，
所以不影响正确性，但会让每次抽取多花一倍时间。改动是在 `_build_payload` 里补一句：
模型名含 `qwen3` 且调用方没显式指定时，默认 `think=False`。

原文件备份：`D:\anaconda3\Lib\site-packages\actants\llm\ollama.py.orig`

想恢复 qwen3 的思考链：删掉 `ollama.py` 里 `if "think" not in payload_extra and "qwen3" in ...`
那三行，或者调用时显式传 `think=True`（显式参数优先）。

### 2. `vector_kb.py`（重写，工作目录）

旧的把报告按段落切块塞进向量库，143 条里大半是 Evidence/Sources/表格这些排版产物。
新的存三类**可追溯、可撤销**的记录：

| 类型 | 内容 | 来源 |
|---|---|---|
| `fact` | 结论 + 原文引文 + 文件 + 页码 | 报告的 Evidence 段，且必须 `[✓]` |
| `avoid` | 什么情况下不要怎么写 | 校验器删句日志 |
| `refuse` | 查过、没有 | 手工 |

**入库闸门**：报告先过 `qc_gate`，不通过一条都不进；正文、Sources、表格一个字都不进。
检索靠 token 重叠（中文二元组 + 英文单词），不做 embedding —— 规则库只有几十到几百条，
关键词够用，而且可解释。

### 3. `qc_gate.py`：按来源类型分档

教材/论文库只有 1 个来源是正常的（一本书就是一源），原来一刀切"至少 3 个来源"
会把合法报告误杀。现在从 Sources 段里认出本地文件路径就走本地档（≥1 源，≥300 字符），
网页报告仍然 ≥3 源、≥600 字符。

### 4. `autonomous_loop.py`：只提问，不擅自记事

- 上下文改成「已确认事实 + 已犯错误」，不再拿旧报告正文当灵感
- 去重从子串比改成 token 余弦（阈值 0.5）——原来的写法几乎永不命中，
  所以同一个问题会被问两遍
- 提问前先查规则库，已有高相关结论（≥0.5）就不重复研究

---

新增依赖：`pip install pypdf sqlite-vec`（已装）。教材库用法见 `...\教材库使用说明.md`。

## 可调开关（不用改代码）

```bash
# 换黑名单（逗号分隔；含 / 的按 URL 子串匹配）
set DEEPDIVE_BLOCK_DOMAINS=blog.csdn.net,wappoc_appmsg

# 允许跨语言 claim 通过（默认 0，即中文 claim 必须配中文引文）
set DEEPDIVE_ALLOW_CROSSLINGUAL=1

# 回到最严格模式：任何没有引用的句子一律删除（默认 0，即"无引用但不引入新事实"可保留）
set DEEPDIVE_DROP_ALL_UNCITED=1

# 相关性门槛（余弦相似度，默认 0.52）。调低 = 更容易硬凑，调高 = 更容易拒答
set DEEPDIVE_MIN_SIM=0.52
```

门槛怎么来的：在 2 篇论文的库上实测，**真正相关的查询最高相似度 0.585~0.595，完全无关的 0.429~0.493**，0.52 落在空档中间。换库之后需要重新校准——拿一个"肯定有答案"的问题和一个"肯定没有"的问题各跑一次，看两者的最高相似度定在中间。

## 句子级校验到底怎么判

| 情况 | 处理 |
|---|---|
| 有引用，且句子里的专名/数字都在所引 claim 里 | 保留 |
| 有引用，但句子提到了 claim 没有的专名/数字（如把 TPO 展开成 Truthful and Precise Output） | **删句** |
| 引用号不存在（如只有 1 个来源却写 `[5]`） | 去掉错号，按"无引用"判定 |
| 无引用，但没引入任何新专名/数字 | 保留（7B 经常忘了给正确答案标出处，全删会误伤） |
| 无引用，且引入了新专名/数字 | **删句** |

数字与术语先归一化再比：`0.1` ≡ `零点一`、`25%` ≡ `百分之二十五`、`Biot` ≡ `毕渥`、`Nu` ≡ `努塞尔`。这张对照表在 `analysis/support.py` 的 `_TERM_PAIRS`，加词直接改那里。

## 验证

```bash
cd /d <项目根目录>
D:\anaconda3\python.exe tests\test_patches.py      # 16 项：去重/标题/引用号/支撑校验/来源过滤/抽取链路
D:\anaconda3\python.exe tests\test_corpus.py       # 15 项：中文断句/分块不变式/PDF 页码/语料检索
D:\anaconda3\python.exe tests\test_synthesis.py    # 19 项：句子级合成校验（脑补/归因/数字/术语/引用号）
```

三套共 **50 项，全绿**。

语料链路的真机验收（跑一次约 75 秒）：

```bash
D:\anaconda3\python.exe tests\make_demo_corpus.py
deepdive index corpus_demo -o corpus_demo\index.db --embed-model bge-m3
deepdive research "多层平壁的总热阻如何计算" --corpus corpus_demo\index.db -m qwen3:8b -o report_corpus_test.md
```

预期：Sources 只有 **1** 条（3 个 chunk 合成一个来源），Evidence 每条带 `· p.1 / p.2 / p.3`，`5/5 grounded`，**句子校验 0 删**（干净的教材问答不该被误伤）。

回归质检闸门：

```bash
D:\anaconda3\python.exe qc_gate.py report_20261018_030157.md
# 拒绝 拦截：正文引用到 [31]，但只有 5 个来源（悬空引用）
```

七份旧报告现在全部被拒——因为它们全是**修复前**的产物，这是预期结果。

## 回滚

```bash
# 方式一：整包还原
cd D:\anaconda3\Lib\site-packages
rm -rf deepdive && cp -r deepdive.orig deepdive

# 方式二：重装
pip install --force-reinstall deepdive_research==0.3.2
```

`deepdive.orig` 是改动前的完整备份，一直保留。

## 升级后如何恢复补丁

`pip install -U deepdive_research` 会覆盖这 9 个文件。届时执行：

```bash
cd /d <项目根目录>
SP="D:/anaconda3/Lib/site-packages/deepdive"
cp -r patches/deepdive_patched/* "$SP/"
D:\anaconda3\python.exe tests\test_patches.py && D:\anaconda3\python.exe tests\test_corpus.py
```

**注意**：本目录里的 `.py` 是改后的完整文件，不是 diff。如果新版本改动了同几处逻辑，直接覆盖可能引入冲突——跑一遍测试即可知道有没有问题。

## 尚未做的一件事

`settings.yml`（SearXNG 配置）**没有动**。里面只开了 baidu / bing / 360search / sogou，这是 CSDN、道客巴巴、词典站的主要来源。

没动的原因：引擎名写错会导致 SearXNG 起不来，整条链路就断了。想改善来源质量，建议你自己在 SearXNG 里启用 `arxiv`、`crossref`、`semantic scholar` 这三个引擎，确认能正常返回 JSON 后再跑 DeepDive。

（教材库那条路完全绕开 SearXNG，所以不受这个影响。）
