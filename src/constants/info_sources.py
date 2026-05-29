"""
非微信公众号的信息源配置。
包括网页来源、搜索引擎 URL、模板模式等。

⚠️ 本文件会提交到 Git。如需保护隐私，请创建 info_sources.py.bk 存放真实配置。
"""

# ============================================================
# 网页信息源（名称, URL, URL匹配pattern）
# 系统会抓取这些网页的文章链接作为候选
# ============================================================

WEB_SOURCES: list[tuple[str, str, str]] = [
    # ("来源名称", "文章列表页URL", "URL匹配pattern"),
    ("机器之心", "https://www.jiqizhixin.com/articles", "jiqizhixin"),
    ("OpenAI Blog", "https://openai.com/blog/", "openai"),
    ("Google DeepMind Blog", "https://deepmind.google/discover/blog/", "deepmind"),
    ("知乎", "https://www.zhihu.com/topic/19550901/hot", "zhihu"),
]

# ============================================================
# WordPress API 源（有公开 JSON API 的站点）
# ============================================================

WP_API_SOURCES: list[dict] = [
    {
        "name": "QbitAI WordPress",
        "url": "https://www.qbitai.com/wp-json/wp/v2/posts?per_page=30&orderby=date&order=desc",
        "display_name": "量子位 / QbitAI",
        "limit": 6,
    },
]

# ============================================================
# GitHub 搜索配置
# ============================================================

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_SEARCH_QUERY = "AI agent context MCP created:>{since}"
GITHUB_SEARCH_PARAMS = "sort=stars&order=desc&per_page=10"
GITHUB_SOURCE_NAME = "GitHub"
GITHUB_MAX_ARTICLES = 2

# ============================================================
# 搜索引擎 URL 模板
# ============================================================

SOGOU_WEB_SEARCH = "https://www.sogou.com/web"
SOGOU_WECHAT_SEARCH = "https://weixin.sogou.com/weixin"
SOGOU_WECHAT_HOME = "https://weixin.sogou.com/"
BING_SEARCH = "https://www.bing.com/search"

# ============================================================
# 来源分类映射（用于 source_bucket）
# ============================================================

SOURCE_BUCKET_MAP: dict[str, str] = {
    "微信公众号": "微信公众号",
    "GitHub": "GitHub",
    # 其余自动归为 "其他"
}

# ============================================================
# 文章类型推断关键词
# ============================================================

GITHUB_TYPE_KEYWORDS = ["github.com", "github"]
TOOL_TYPE_KEYWORDS = ["sdk", "api", "mcp", "context", "harness", "openai"]

# ============================================================
# 领域洞察模板（用于 LLM 不可用时的 fallback）
# ============================================================

INSIGHTS: dict[str, str] = {
    "AI Agent": "值得关注它是否把规划、工具调用、反馈评估做成闭环，而不是停留在聊天式能力展示。",
    "Harness Engineering": "这类进展说明 AI 落地正在进入工程约束阶段，稳定性、成本和可观测性会决定真实价值。",
    "Context Engineering": "上下文组织正在成为 Agent 质量上限，检索、记忆和权限边界需要作为同一套系统设计。",
    "MCP": "MCP 相关动态会影响 Agent 能连接多少真实系统，是从演示走向生产的关键基础设施。",
    "AI Coding": "Coding Agent 进入真实仓库后，权限、测试、审查和供应链安全需要和生成能力同步建设。",
    "Vibe Coding": "Vibe Coding 的价值不只在生成速度，更在能否把需求、实现、验证串成可追踪流程。",
    "LLM推理与优化": "推理优化正在从实验室走向工程实践，模型能力上限和实际可用性之间的鸿沟在缩小。",
    "多模态大模型": "多模态能力扩展了 AI 的感知边界，但真正的挑战在于不同模态之间的对齐和可靠性。",
}

# ============================================================
# 辅助函数
# ============================================================


def get_web_sources() -> list[tuple[str, str, str]]:
    """返回网页信息源列表。"""
    return WEB_SOURCES


def get_wp_api_sources() -> list[dict]:
    """返回 WordPress API 源列表。"""
    return WP_API_SOURCES


def build_github_url(since: str) -> str:
    """根据日期构建 GitHub 搜索 URL。"""
    query = GITHUB_SEARCH_QUERY.format(since=since)
    from urllib.parse import quote
    return f"{GITHUB_SEARCH_URL}?q={quote(query)}&{GITHUB_SEARCH_PARAMS}"


def get_source_bucket(source: str) -> str:
    """判断来源类型。"""
    for key, bucket in SOURCE_BUCKET_MAP.items():
        if key in source:
            return bucket
    return "其他"


def get_insight(category: str) -> str:
    """根据分类获取领域洞察。"""
    return INSIGHTS.get(category, INSIGHTS.get("AI Agent", ""))
