"""
微信公众号搜索源配置。
请将下方的示例名称替换为你想要追踪的公众号。

格式说明：
- WECHAT_SOURCES: list[tuple[str, str]] — (公众号名称, 搜索关键词)
  公众号名称用于搜狗微信搜索精确匹配，搜索关键词用于内容筛选
- WECHAT_ACCOUNT_IDS: dict[str, str] — {公众号名称: 搜狗Account ID}
  Account ID 可选填，用于搜狗微信搜索的 account_id 精确匹配参数
  如果不知道 Account ID，留空字符串即可，系统会用公众号名称搜索
- FORBIDDEN_PATTERNS: list[tuple[str, str]] — (模式名, 正则)
  生成的文章中禁止出现这些来源名称，防止搬运腔
- CONTEXT_NOISE_PATTERNS: list[str] — 正文清洗时要过滤的噪声行

⚠️ 本文件会提交到 Git。如需保护隐私，请创建 wechat_sources.py.bk 存放真实配置。
"""

import re

# ============================================================
# 请在这里填入你要追踪的微信公众号
# ============================================================

WECHAT_SOURCES: list[tuple[str, str]] = [
    # ("公众号名称", "搜索关键词") — 搜狗微信搜索直接用公众号名，无需额外关键词
    ("ChallengeHub", ""),
    ("量子位", ""),
    ("Ai学习的老章", ""),
]

WECHAT_ACCOUNT_IDS: dict[str, str] = {
    # "公众号名称": "Account ID（可选，不知道就留空）",
    "ChallengeHub": "",
    "量子位": "QbitAI",
    "Ai学习的老章": "",
}

# ============================================================
# 以下函数从上方配置自动生成禁止词和噪声模式
# 无需手动修改
# ============================================================


def build_forbidden_patterns() -> list[tuple[str, str]]:
    """从 WECHAT_SOURCES 自动生成禁止词列表。

    规则设计原则：
    - 只拦截"搬运腔"用法（如"据量子位报道""量子位称""援引机器之心"等），
      不拦截"正常提及"（如"量子位举办了xxx会议""在机器之心的论坛上"等）
    - 通用禁止词（参考资料、参考链接等）保留硬性拦截
    - 信息源名称不在此处硬编码，而是从 WEB_SOURCES 自动生成搬运腔模式
    """
    from src.constants.info_sources import get_web_sources

    patterns: list[tuple[str, str]] = []

    # 通用禁止词（不依赖具体来源名）
    patterns.extend([
        ("参考资料", r"参考资料"),
        ("参考链接", r"参考链接"),
        ("来源归因", r"来源[:：]"),
    ])

    # 搬运腔模式：只拦截"据{来源名}报道/称/消息"这类表述
    # 而放行"{来源名}举办了""{来源名}发布了"等正常新闻叙述
    all_source_names: set[str] = set()
    for name, _keyword in WECHAT_SOURCES:
        all_source_names.add(name)
    for name, _url, _pattern in get_web_sources():
        all_source_names.add(name)

    for name in all_source_names:
        escaped = re.escape(name)
        # 匹配搬运腔：来源名作为"消息源"被引用的各种表述
        # - 前缀型：据/援引/来自/根据/按照 + 来源名 + （的）+ 报道/称/消息/表示/指出/发布/发文/文章
        # - 后缀型：来源名 + 报道/称/表示/指出/发文称/发文（来源名直接做报道主语）
        patterns.append((
            f"搬运腔-{name}",
            rf"(?:据|援引|来自|根据|按照)\s*{escaped}\s*(?:的\s*)?(?:报道|称|消息|表示|指出|发布|发文|文章)"
            rf"|(?<!\w){escaped}\s*(?:发文称|发文|报道|称|表示|指出)"
        ))

    return patterns


def build_context_noise_patterns() -> list[str]:
    """从 WECHAT_SOURCES 自动生成上下文噪声模式。"""
    patterns: list[str] = []
    for name, _keyword in WECHAT_SOURCES:
        patterns.append(name)
    # 追加通用噪声模式
    patterns.extend([
        "来源：", "扫码", "相关阅读",
        "参考链接", "热门文章", "版权所有", "ICP备",
        "关于我们", "加入我们", "商务合作", "首页",
    ])
    return patterns
