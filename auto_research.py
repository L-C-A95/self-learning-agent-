import os
import sys
import subprocess
import datetime
from pathlib import Path
from qc_gate import check_report_quality

# 配置
WORK_DIR = str(Path(__file__).resolve().parent)
DEEPDIVE_CMD = r"D:\anaconda3\Scripts\deepdive.exe"  # 如果路径不同，用 where deepdive 查一下
MODEL = os.environ.get("DEEPDIVE_MODEL", "qwen3:8b")
SEARCH_BACKEND = "searxng"
KB_SCRIPT = os.path.join(WORK_DIR, "vector_kb.py")
QA_DIR = os.path.join(WORK_DIR, "产出", "问答记录")

# 来源黑名单。含 "/" 的条目按 URL 子串匹配（需要补丁 P4），其余按域名匹配。
# 想改就直接改这个默认值，或设环境变量 DEEPDIVE_BLOCK_DOMAINS 覆盖。
# 说明：mp.weixin.qq.com 没有被封，只封反爬页特征（wappoc_appmsg / captcha），
#      这样中文选题仍然能用公众号文章，但验证码页进不来。
BLOCK_DOMAINS = os.environ.get(
    "DEEPDIVE_BLOCK_DOMAINS",
    "blog.csdn.net,dict.cn,doc88.com,wenku.baidu.com,"
    "arxiv.org/search,wappoc_appmsg,captcha",
)

def run_research(topic):
    """调用 DeepDive 研究一个主题，生成报告"""
    date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(QA_DIR, exist_ok=True)
    report_file = os.path.join(QA_DIR, f"网页研究_{date_str}.md")

    print(f"\n{'='*60}", flush=True)
    print(f"开始研究: {topic}", flush=True)
    print(f"报告将保存到: {report_file}", flush=True)
    print(f"{'='*60}\n", flush=True)

    cmd = [
        DEEPDIVE_CMD, "research", topic,
        "-m", MODEL,
        "-b", SEARCH_BACKEND,
        "-o", report_file,
        "--force"
    ]
    if BLOCK_DOMAINS:
        cmd += ["--block-domains", BLOCK_DOMAINS]

    result = subprocess.run(cmd, cwd=WORK_DIR, capture_output=False, text=True)
    if result.returncode != 0:
        print(f"研究失败，返回码: {result.returncode}", flush=True)
        return None

    if not os.path.exists(report_file):
        print(f"报告文件未生成", flush=True)
        return None

    print(f"\n报告已生成: {report_file}", flush=True)
    return report_file

def save_to_kb(report_file, topic):
    """把报告里**通过校验的事实**存进规则库。

    不是把报告正文塞进去 —— 正文、Sources、表格都是排版产物，不是知识。
    """
    print(f"\n正在提取可复用结论...", flush=True)
    cmd = [sys.executable, KB_SCRIPT, "add", report_file, topic]
    result = subprocess.run(cmd, cwd=WORK_DIR, capture_output=False, text=True)
    if result.returncode != 0:
        print(f"入库失败，返回码: {result.returncode}", flush=True)
        return False
    return True

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python auto_research.py <研究主题>")
        sys.exit(1)

    topic = sys.argv[1]
    report = run_research(topic)
    
    if report:
        # --- 核心修改：入库前先质检 ---
        print(f"\n🛡️ 正在对报告进行安检...", flush=True)
        is_good, reason = check_report_quality(report)
        
        if is_good:
            print(f"✅ 安检通过: {reason}", flush=True)
            save_to_kb(report, topic)
            print(f"\n全部完成！主题: {topic}", flush=True)
        else:
            print(f"❌ 安检未通过: {reason}", flush=True)
            print("报告被拦截，已丢弃，不存入知识库。", flush=True)
            # 可选：把垃圾报告移动到垃圾箱文件夹，或者直接删掉
            os.rename(report, report + ".rejected")