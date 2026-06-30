"""配置管理（模板文件，配置信息由用户自行填写）"""

# 模型配置（可选，用于智能建议）
MODEL_PROVIDER = ""
MODEL_NAME = ""
API_BASE = ""
API_KEY = ""

# 飞书推送（可选）
FEISHU_APP_ID = ""
FEISHU_APP_SECRET = ""
FEISHU_REVIEW_CHAT_ID = ""

# 输出目录
REPORT_OUTPUT_DIR = "workspace/agent/reports/data-expert"

# 数据清洗默认参数
DEFAULT_MISSING_THRESHOLD = 0.3  # 缺失率超过此值的列将被删除
