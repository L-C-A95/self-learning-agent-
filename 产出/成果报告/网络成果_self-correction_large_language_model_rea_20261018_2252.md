# 成果报告：self-correction large language model reasoning

_生成于 2026-10-18 22:52 · 2 篇论文（2024–2025）· 8 条可核验知识点_

> 判断和事实分开写：标 **论文结论** 的是论文原文说的，标 **推断** 的是基于这些结论外推的，我自己也不确定。


## 一、知识点总结（按论文）


### Large Language Models have Intrinsic Self-Correction Ability
`2024 · arXiv:2406.15673v2 · 有代码/数据线索、有实验或仿真、偏理论/综述`

- **本文通过理论分析和实验验证，证明了大语言模型（LLMs）具有内在的自我校正能力。** · p.1
  > In this paper, we present a novel perspective on the intrinsic self-correction capabilities of LLMs through theoretical analyses and empirical experiments.
- **LLMs在首次回答时无法正确回答问题，这是由于其内在的幻觉特性导致的。** · p.2
  > In addition, we also show that LLMs cannot answer questions correctly during the initial attempt because of their inherent hallucination property.
- **内在自我校正能力的发挥受到温度参数和提示语公平性两个关键因素的影响。** · p.2
  > Through theoretical analyses and empirical experiments, we identify two factors impacting the intrinsic SC ability of LLMs.

### On the Convergence of Moral Self-Correction in Large Language Models
`2025 · arXiv:2510.07290v3 · 有代码/数据线索、有实验或仿真、偏理论/综述`

- **论文揭示了道德自我纠正过程中，通过多轮交互实现性能收敛的关键特性。** · p.1
  > Focusing on moral self-correction in LLMs, we reveal a key characteristic of intrin- sic self-correction: performance convergence through multi-round interactions; and provide a mechanistic analysis of this convergence b
- **道德自我纠正指令激活了与道德相关的潜在概念，从而减少模型的不确定性，最终实现性能收敛。** · p.1
  > Based on our experimental results and analysis, we uncover the underlying mecha- nism of convergence: consistently injected self- correction instructions activate moral concepts that reduce model uncertainty, leading to 
- **论文通过实验验证了多轮内在自我纠正可以在不同任务和模型中实现性能收敛。** · p.1
  > There are two main research ques- tions concerning general intrinsic self-correction and moral self-correction:RQ1:Can the iterative application of intrinsic self-correction achieve con- verged performance?This convergen
- **模型的不确定性反映了其输出语言变化的程度，随着不确定性减少，生成的响应趋于稳定。** · p.2
  > Notably, model uncertainty reflects the degree of linguis- tic variation in the model’s outputs (Malinin and Gales, 2021; Kuhn et al., 2022; Huang et al., 2024; Chang et al., 2024; Zhang et al., 2024).
- **通过多轮自我纠正，模型的毒性水平逐渐下降并最终趋于收敛。** · p.2
  > By injecting self-correction instructions (bold font) into queries (green text boxes) for several rounds, the toxicity level of generated sentences (blue text boxes) decline and ultimately approach convergence.

## 二、横向对比（同一问题的不同做法）


**LLMs的自我校正能力**
涉及：Large Language Models have…、On the Convergence of Moral…
- 差异：k1强调理论分析和实验验证，k4聚焦多轮交互实现性能收敛
  - [k1] 本文通过理论分析和实验验证，证明了大语言模型（LLMs）具有内在的自我校正能力。
  - [k4] 论文揭示了道德自我纠正过程中，通过多轮交互实现性能收敛的关键特性。

**自我校正的影响因素**
涉及：Large Language Models have…、On the Convergence of Moral…
- 差异：k3关注温度参数和提示语公平性，k7强调模型不确定性与响应稳定性的关系
  - [k3] 内在自我校正能力的发挥受到温度参数和提示语公平性两个关键因素的影响。
  - [k7] 模型的不确定性反映了其输出语言变化的程度，随着不确定性减少，生成的响应趋于稳定。

**道德自我纠正机制**
涉及：On the Convergence of Moral…
- 差异：k5强调道德指令减少不确定性，k8关注毒性水平随多轮纠正下降
  - [k5] 道德自我纠正指令激活了与道德相关的潜在概念，从而减少模型的不确定性，最终实现性能收敛。
  - [k8] 通过多轮自我纠正，模型的毒性水平逐渐下降并最终趋于收敛。

**多轮自我纠正效果**
涉及：On the Convergence of Moral…
- 差异：k4关注道德自我纠正的收敛特性，k6强调多轮纠正在不同任务和模型中的通用性
  - [k4] 论文揭示了道德自我纠正过程中，通过多轮交互实现性能收敛的关键特性。
  - [k6] 论文通过实验验证了多轮内在自我纠正可以在不同任务和模型中实现性能收敛。

## 三、纵向演进（按年份）

- **2024** · Large Language Models have Intrinsic Self-Correction Ability
  → 本文通过理论分析和实验验证，证明了大语言模型（LLMs）具有内在的自我校正能力。
- **2025** · On the Convergence of Moral Self-Correction in Large Language Models
  → 论文揭示了道德自我纠正过程中，通过多轮交互实现性能收敛的关键特性。

> 注意：这里只按 arXiv 提交年份排序，**不代表这个方向的最早工作**。更早的经典论文大多在期刊上，arXiv 没有。

## 四、值得往哪走


**探索温度参数对模型自我校正能力的具体影响机制**
- 性质：推断
- 依据：k3
  - [k3] 内在自我校正能力的发挥受到温度参数和提示语公平性两个关键因素的影响。
- 怎么验证：设计不同温度参数下的实验，观察模型在多轮交互中的自我校正效果变化

**研究提示语公平性对模型道德自我纠正能力的影响**
- 性质：推断
- 依据：k3
  - [k3] 内在自我校正能力的发挥受到温度参数和提示语公平性两个关键因素的影响。
- 怎么验证：在不同提示语公平性设置下，测试模型在道德任务中的性能收敛速度

**分析多轮交互中模型毒性水平下降的动态过程**
- 性质：推断
- 依据：k8
  - [k8] 通过多轮自我纠正，模型的毒性水平逐渐下降并最终趋于收敛。
- 怎么验证：通过记录多轮交互中模型输出的毒性指标变化，分析其收敛趋势

**研究道德相关指令如何影响模型的不确定性**
- 性质：推断
- 依据：k5
  - [k5] 道德自我纠正指令激活了与道德相关的潜在概念，从而减少模型的不确定性，最终实现性能收敛。
- 怎么验证：在不同道德指令下，测量模型输出的不确定性变化并分析其与性能收敛的关系

## 五、这份报告没做到的

- **只覆盖 arXiv。** 期刊论文（Elsevier / ASHRAE / 知网）不在里面，所以「这个方向只有这些工作」不成立，只能说「arXiv 上有这些」。
- **没有引用数。** 影响力需要 Semantic Scholar 才能看，这次没接。
- **可实践性只是线索，不是结论。** 标「有代码/数据线索」只说明论文里提到过，不代表真能跑起来。