"""自主浏览的学科清单。

选这些的依据只有一条：**arXiv 上真有全文。**
所以没有"临床医学""法学""建筑给排水"——那些在别的库里，抓不到全文，
让 agent 去搜只会得到一堆不相关的二手转述。

排在前两个的是你的专业方向（能源、传热），这样它学到的东西对你更有用。
"""

FIELDS: list[tuple[str, str]] = [
    ("能源与储能", "phase change material thermal energy storage"),
    ("传热与流体", "convection heat transfer turbulent boundary layer"),
    ("计算机 · 机器学习", "language model reasoning"),
    ("计算机 · 系统与网络", "distributed system consensus latency"),
    ("物理 · 量子信息", "quantum error correction surface code"),
    ("物理 · 凝聚态", "topological insulator transport"),
    ("材料科学", "metal organic framework adsorption"),
    ("生物 · 神经科学", "neural population coding cortex"),
    ("环境与气候", "climate model precipitation extremes"),
    ("数学", "random matrix eigenvalue distribution"),
    ("经济与博弈", "mechanism design auction"),
    ("交叉 · 科学计算", "physics informed neural network solver"),
]
