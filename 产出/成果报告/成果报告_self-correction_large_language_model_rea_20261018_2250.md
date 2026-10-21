# 成果报告：self-correction large language model reasoning

_生成于 2026-10-18 22:50 · 3 篇论文（2023–2025）· 9 条可核验知识点_

> 判断和事实分开写：标 **论文结论** 的是论文原文说的，标 **推断** 的是基于这些结论外推的，我自己也不确定。


## 一、知识点总结（按论文）


### Large Language Models Cannot Self-Correct Reasoning Yet
`2023 · arXiv:2310.01798 · 有代码/数据线索`

- **该论文发现大型语言模型在没有外部反馈的情况下难以自我纠正其推理过程，甚至在自我纠正后性能会下降。** · p.1
  > In the context of reasoning, our research indicates that LLMs struggle to self- correct their responses without external feedback, and at times, their performance even degrades after self-correction.
- **该论文指出，现有研究中自我纠正性能的提升往往依赖于理想化的‘oracle标签’，而实际情况下这些标签不可用。** · p.2
  > Upon closer examination, we observe that the improvements in these studies result from using oracle labels to guide the self-correction process, and the improvements vanish when oracle labels are not available.
- **该论文强调，自我纠正过程中需要设计有效的提示（prompt），以确保模型能够基于自身能力进行修正。**
  > Specifically, each self- correction process involves designing prompts for both the initial response generation and the self- correction steps. Our evaluation reveals that the self-correction improvement claimed by some 

### Small Language Models Need Strong Verifiers to Self-Correct Reasoning
`2024 · arXiv:2404.17140 · 有代码/数据线索、有实验或仿真`

- **该论文提出了一种名为SCORE的方法，通过小模型自身生成的批判性数据来提升其自我修正能力。** · p.1
  > We propose SCORE —an approach to bootstrap small LMs’ Self-COrrection ability in REasoning tasks. Concretely, we devise a pipeline for accumulating high-quality critique-correction data from small LMs, which are used for
- **实验结果表明，在使用GPT-4作为验证器的情况下，经过SCORE微调的小模型在数学和常识推理任务上平均提升了14.6%。** · p.2
  > We test the SCORE method with the LLaMA-2-13B-chat (Touvron et al., 2023) and Gemma-7b-it (Team et al., 2024) models on five datasets spanning math and commonsense reason- ing. We find that our model with SCORE fine- tun
- **该论文指出，小模型在缺乏强验证器的情况下，自我修正能力较弱，尤其是在使用弱验证器微调后表现不佳。**
  > Nevertheless, the model struggles with self- correction when subjected to a weak self-verifier fine-tuned on self-generated solutions. Identifying when to self-correct involves verifying the solu- tions’ correctness, whi

### Sherlock: Self-Correcting Reasoning in Vision-Language Models
`2025 · arXiv:2505.22651 · 有代码/数据线索`

- **Sherlock通过引入轨迹级自我纠正目标和动态β参数，使模型在仅使用20k标注数据的情况下实现自我纠正和持续改进。** · p.1
  > Based on our findings, we introduceSherlock, a self-correction and self-improvement training framework.Sherlockintroduces a trajectory-level self-correction objective, a preference data construction method based on visua
- **现有推理VLMs在自我纠正方面表现不佳，仅能在少于10%的样本中实现自我纠正，且仅有半数能得出正确答案。**
  > We find that these models generallycannotself-correct. We find that these models generallycannotself-correct.
- **Sherlock通过视觉扰动构建偏好数据集，并利用自我纠正前后的响应形成偏好对，从而减少对大量标注数据的依赖。** · p.2
  > Additionally, it leverages visual noise perturbations to construct preference datasets with controllable quality gaps. Moreover, the responses before and after self-correction naturally form a preference pair [5, 52], wh

## 二、横向对比（同一问题的不同做法）


**自我纠正依赖理想化标签**
涉及：Large Language Models Cannot…、Small Language Models Need…
- 差异：k2依赖理想化标签，k6强调弱验证器效果差
  - [k2] 该论文指出，现有研究中自我纠正性能的提升往往依赖于理想化的‘oracle标签’，而实际情况下这些标签不可用。
  - [k6] 该论文指出，小模型在缺乏强验证器的情况下，自我修正能力较弱，尤其是在使用弱验证器微调后表现不佳。

**提升自我纠正能力的方法**
涉及：Large Language Models Cannot…、Small Language Models Need…
- 差异：k3强调设计提示，k4使用小模型生成数据
  - [k3] 该论文强调，自我纠正过程中需要设计有效的提示（prompt），以确保模型能够基于自身能力进行修正。
  - [k4] 该论文提出了一种名为SCORE的方法，通过小模型自身生成的批判性数据来提升其自我修正能力。

**自我纠正效果评估**
涉及：Large Language Models Cannot…、Sherlock: Self-Correcting…
- 差异：k1关注模型自我纠正失败，k8关注实际自我纠正效果
  - [k1] 该论文发现大型语言模型在没有外部反馈的情况下难以自我纠正其推理过程，甚至在自我纠正后性能会下降。
  - [k8] 现有推理VLMs在自我纠正方面表现不佳，仅能在少于10%的样本中实现自我纠正，且仅有半数能得出正确答案。

**减少标注数据依赖**
涉及：Sherlock: Self-Correcting…
- 差异：k7用轨迹级目标，k9用视觉扰动构建偏好对
  - [k7] Sherlock通过引入轨迹级自我纠正目标和动态β参数，使模型在仅使用20k标注数据的情况下实现自我纠正和持续改进。
  - [k9] Sherlock通过视觉扰动构建偏好数据集，并利用自我纠正前后的响应形成偏好对，从而减少对大量标注数据的依赖。

## 三、纵向演进（按年份）

- **2023** · Large Language Models Cannot Self-Correct Reasoning Yet
  → 该论文发现大型语言模型在没有外部反馈的情况下难以自我纠正其推理过程，甚至在自我纠正后性能会下降。
- **2024** · Small Language Models Need Strong Verifiers to Self-Correct Reasoning
  → 该论文提出了一种名为SCORE的方法，通过小模型自身生成的批判性数据来提升其自我修正能力。
- **2025** · Sherlock: Self-Correcting Reasoning in Vision-Language Models
  → Sherlock通过引入轨迹级自我纠正目标和动态β参数，使模型在仅使用20k标注数据的情况下实现自我纠正和持续改进。

> 注意：这里只按 arXiv 提交年份排序，**不代表这个方向的最早工作**。更早的经典论文大多在期刊上，arXiv 没有。

## 四、值得往哪走


**开发无需强验证器的小模型自我纠正方法**
- 性质：推断
- 依据：k6
  - [k6] 该论文指出，小模型在缺乏强验证器的情况下，自我修正能力较弱，尤其是在使用弱验证器微调后表现不佳。
- 怎么验证：尝试在无强验证器的情况下，设计小模型自我纠正机制并测试其性能

**利用小模型自身生成的批判性数据提升自我纠正能力**
- 性质：论文结论
- 依据：k4
  - [k4] 该论文提出了一种名为SCORE的方法，通过小模型自身生成的批判性数据来提升其自我修正能力。
- 怎么验证：复现SCORE方法，并测试其在不同任务和数据集上的效果

**通过视觉扰动构建偏好数据集以减少标注依赖**
- 性质：论文结论
- 依据：k9
  - [k9] Sherlock通过视觉扰动构建偏好数据集，并利用自我纠正前后的响应形成偏好对，从而减少对大量标注数据的依赖。
- 怎么验证：尝试使用视觉扰动生成偏好数据集，并评估其在自我纠正任务中的有效性

**探索动态参数调整对自我纠正性能的影响**
- 性质：推断
- 依据：k7
  - [k7] Sherlock通过引入轨迹级自我纠正目标和动态β参数，使模型在仅使用20k标注数据的情况下实现自我纠正和持续改进。
- 怎么验证：设计动态β参数调整机制，并在不同数据集上测试其对自我纠正效果的影响

## 五、这份报告没做到的

- **只覆盖 arXiv。** 期刊论文（Elsevier / ASHRAE / 知网）不在里面，所以「这个方向只有这些工作」不成立，只能说「arXiv 上有这些」。
- **没有引用数。** 影响力需要 Semantic Scholar 才能看，这次没接。
- **可实践性只是线索，不是结论。** 标「有代码/数据线索」只说明论文里提到过，不代表真能跑起来。