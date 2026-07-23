"""标签归一化（第一层语义处理）。

问题：用户自由填写时，"AI Agent"/"人工智能代理"/"智能体" 会被算成三个不相干的标签，
兴趣重合度被严重低估。

方案：一张手工同义词表，把自由标签折叠成规范标签再算重合度。
纯字典查找，零延迟、确定性、可复现——不引入 LLM，符合"匹配路径不含 LLM"的红线。
（LLM 只在录入阶段把自然语言转成标签，那一步在 profile 写入时完成，不在此处。）

维护方式：现场遇到没覆盖的标签，直接往 SYNONYMS 里加一行即可。
"""
from __future__ import annotations

import re

# 规范标签 -> 所有等价写法（小写比较）
SYNONYMS: dict[str, list[str]] = {
    "ai-agent": [
        "ai agent", "aiagent", "agent", "人工智能代理", "智能体", "智能代理",
        "llm agent", "ai助手", "autonomous agent",
    ],
    "llm": [
        "large language model", "大语言模型", "大模型", "gpt", "claude",
        "chatgpt", "语言模型",
    ],
    "hw-wearable": [
        "wearable", "可穿戴", "可穿戴设备", "智能硬件", "smart hardware",
        "智能穿戴", "hardware", "硬件", "iot", "物联网", "嵌入式", "embedded",
    ],
    "robotics": ["机器人", "robot", "具身智能", "embodied ai", "机械臂"],
    "frontend": ["前端", "web开发", "react", "vue", "ui开发"],
    "backend": ["后端", "服务端", "server", "api开发"],
    "design": ["设计", "ui", "ux", "交互设计", "产品设计", "视觉设计"],
    "product": ["产品", "产品经理", "pm", "product manager"],
    "startup": ["创业", "创业公司", "独立开发", "indie hacker", "solo founder"],
    "sci-fi": [
        "科幻", "科幻电影", "科幻小说", "scifi", "science fiction",
        "三体", "赛博朋克", "cyberpunk",
    ],
    "music-electronic": [
        "电子音乐", "edm", "techno", "house", "电音", "蹦迪", "clubbing",
        "dj", "rave",
    ],
    "music-rock": ["摇滚", "rock", "乐队", "band", "livehouse", "现场演出"],
    "anime": ["动漫", "二次元", "漫展", "cosplay", "acg", "manga"],
    "gaming": ["游戏", "电竞", "game", "esports", "独立游戏", "indie game"],
    "photography": ["摄影", "拍照", "camera", "胶片", "film photography"],
    "outdoor": ["户外", "徒步", "hiking", "露营", "camping", "登山", "骑行"],
    "fitness": ["健身", "运动", "gym", "跑步", "running", "攀岩", "bouldering"],
    "film": ["电影", "movie", "看片", "影迷", "cinema"],
    "reading": ["阅读", "读书", "book", "文学", "看书"],
    "coffee": ["咖啡", "手冲", "espresso", "精品咖啡"],
    "travel": ["旅行", "旅游", "backpacking", "穷游"],
}

# 反向索引：等价写法 -> 规范标签（构建时展开）
_LOOKUP: dict[str, str] = {}
for _canon, _variants in SYNONYMS.items():
    _LOOKUP[_canon] = _canon
    for _v in _variants:
        _LOOKUP[_v.lower()] = _canon


def _clean(raw: str) -> str:
    """去空白、统一分隔符、转小写。"""
    s = raw.strip().lower()
    s = re.sub(r"[\s_]+", " ", s)
    s = s.replace("-", " ") if s not in _LOOKUP else s
    return s.strip()


def normalize_tag(raw: str) -> str:
    """单个标签归一化。表里没有的原样保留（清洗后），不丢弃用户输入。"""
    if not raw:
        return ""
    s = raw.strip().lower()
    if s in _LOOKUP:
        return _LOOKUP[s]
    c = _clean(raw)
    if c in _LOOKUP:
        return _LOOKUP[c]
    # 尝试把空格换成连字符再查（"ai agent" vs "ai-agent"）
    hyphen = c.replace(" ", "-")
    if hyphen in _LOOKUP:
        return _LOOKUP[hyphen]
    return hyphen or c


def normalize_tags(raw_tags: list[str]) -> list[str]:
    """列表归一化并去重，保持稳定顺序（可复现）。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in raw_tags:
        n = normalize_tag(t)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return sorted(out)


def display_shared(a_tags: list[str], b_tags: list[str]) -> list[str]:
    """返回规范化后的共同标签，用于社交卡展示。"""
    return sorted(set(normalize_tags(a_tags)) & set(normalize_tags(b_tags)))


# ---------------- 领域层级：给"相邻概念"部分credit ----------------
# 同义词表只能折叠完全同义的写法（AI Agent = 人工智能代理）。
# 但 "ai-agent" 和 "llm" 是相邻概念——做 Agent 的人和做大模型的人
# 显然该被撮合，字面 Jaccard 却算出 0。
# 解法：给同领域不同标签一个部分分（DOMAIN_CREDIT），确定性、可解释。
DOMAINS: dict[str, str] = {
    "ai-agent": "ai", "llm": "ai", "robotics": "ai",
    "frontend": "build", "backend": "build", "design": "build",
    "product": "build", "startup": "build", "hw-wearable": "build",
    "music-electronic": "music", "music-rock": "music",
    "anime": "culture", "sci-fi": "culture", "film": "culture",
    "gaming": "culture", "reading": "culture",
    "outdoor": "active", "fitness": "active", "travel": "active",
    "photography": "craft", "coffee": "craft",
}

DOMAIN_CREDIT = 0.4      # 同领域不同标签算 0.4 个重合


def soft_overlap(a_tags: list[str], b_tags: list[str]) -> float:
    """带领域部分分的重合度，取值 0-1，可退化为普通 Jaccard。

    完全相同的标签算 1.0，同领域不同标签算 DOMAIN_CREDIT，
    分母仍是并集大小，保证结果不超过 1。
    """
    sa, sb = set(normalize_tags(a_tags)), set(normalize_tags(b_tags))
    if not sa or not sb:
        return 0.0

    exact = sa & sb
    total = float(len(exact))

    # 剩余标签按领域配对，每个标签最多用一次
    rest_a = sorted(sa - exact)
    rest_b = sorted(sb - exact)
    used_b: set[str] = set()
    for ta in rest_a:
        da = DOMAINS.get(ta)
        if da is None:
            continue
        for tb in rest_b:
            if tb in used_b:
                continue
            if DOMAINS.get(tb) == da:
                total += DOMAIN_CREDIT
                used_b.add(tb)
                break

    union = len(sa | sb)
    return min(1.0, total / union) if union else 0.0
