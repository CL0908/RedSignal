"""小黑 —— App 内置的产品助手。

只回答"这个 App 是什么、怎么用、为什么值得认识新人"这类问题。
明确不碰的：技术实现、后端架构、密钥配置、已知缺陷、其他用户的隐私、
以及任何与产品无关的通用问答（那是通用聊天机器人的活，不是这里的）。

没有 ANTHROPIC_API_KEY 时走关键词兜底，断网也能答上几句常见问题——
现场网络不稳时不至于开天窗。
"""
from __future__ import annotations

import logging
import re

from . import agent, config

log = logging.getLogger("redsignal.bot")

GREETING = "hi！我是小黑。有什么可以帮到你？"

SYSTEM = """你是「小黑」，RedSignal（红信号）这款 App 里的产品助手。

RedSignal 是什么：一个线下轻社交产品。你在活动现场打开它，填上自己的兴趣
标签和「今天想找什么样的人」，它会按兴趣重合度、社交目标、沟通方式帮你找到
合得来的人。双方都表示愿意认识，才会交换彼此预先授权的社交资料，
然后有一位 AI 破冰官给你们递上第一个话题。

你的职责：
- 用一两句话回答「怎么匹配的」「为什么要认识新人」「红绿蓝是什么意思」
  「标签怎么填」「资料会不会泄露」这类基本问题。
- 语气自然、简短、像朋友。中文回答。不要用项目符号，不要长篇大论。
- 鼓励对方去认识新的人，但不说教。

你绝不做的事：
- 不谈技术实现、系统架构、数据库、密钥、部署、代码。
  被问到就说「这个我不太清楚，我只管帮你认识人」。
- 不评价产品的缺点、不足、bug、限制，也不承认任何"做不到"的具体细节。
  被追问就把话题带回怎么用。
- 不透露任何其他用户的信息。
- 不回答与这个 App 无关的通用问题（写代码、算数、时事等），
  礼貌地说这不是你负责的。
- 不回答敏感话题：政治、宗教、色情、医疗建议、法律建议、投资建议。

回答控制在 60 字以内。"""

# 断网/无 key 时的兜底。**顺序即优先级**：具体的话题必须排在
# 「为什么/干嘛」这类通用疑问词之前，否则「戒指是干嘛的」会被通用规则抢走。
_FALLBACK = [
    (r"你是谁|你叫|小黑",
     "我是小黑，这个 App 里帮你认识新朋友的小助手。"),
    (r"戒指|硬件|手环",
     "戒指是用来确认的——按一下就代表你愿意认识对方。没有戒指也能用。"),
    (r"红|绿|蓝|模式|颜色",
     "红色是想遇见心动的人，绿色是想认识同好，蓝色是暂时不想被打扰。"
     "只有选同一个颜色的人之间才会匹配。"),
    (r"标签|怎么填|填什么",
     "写你真正在做或真正喜欢的事，越具体越好。"
     "「AI Agent」比「科技」有用得多。"),
    (r"隐私|安全|泄露|资料|信息",
     "双方都确认愿意认识之前，谁也看不到对方是谁。"
     "交换的也只有你自己勾选过的那些内容。"),
    (r"聊天|消息|对话",
     "匹配成功之后就能聊了，会话只存在于你们两个人之间。"),
    (r"匹配|怎么找|推荐|算法",
     "我看你的兴趣标签和「今天想找」，跟现场其他人比对，"
     "兴趣越合、想找的东西越对得上，就排得越前面。"),
    (r"为什么|意义|干嘛|有什么用",
     "线下场合最难的是开口第一句。我先帮你找到聊得来的人，"
     "再给你们递个话题，剩下的就好办了。"),
]

_REFUSE_TOPICS = re.compile(
    r"政治|宗教|色情|投资|股票|诊断|吃什么药|法律责任|怎么起诉", re.I)
_REFUSE_TECH = re.compile(
    r"源码|代码|架构|数据库|api\s*key|密钥|token|部署|服务器|漏洞|bug|缺陷", re.I)


def _fallback(question: str) -> str:
    if _REFUSE_TOPICS.search(question):
        return "这个我就不聊了。我在行的是帮你在现场认识合得来的人。"
    if _REFUSE_TECH.search(question):
        return "这个我不太清楚，我只管帮你认识人。要不要先去「附近」看看？"
    for pattern, answer in _FALLBACK:
        if re.search(pattern, question):
            return answer
    return ("我主要能聊这个 App 怎么用——比如怎么匹配、标签怎么填、"
            "红绿蓝是什么意思。想问哪个？")


def reply(question: str) -> str:
    """回一句。任何异常都退回兜底，绝不把错误抛给用户。"""
    q = (question or "").strip()[:300]
    if not q:
        return GREETING
    # 敏感与技术类问题不送 LLM，直接按规则挡掉——省一次调用，也更可控
    if _REFUSE_TOPICS.search(q) or _REFUSE_TECH.search(q):
        return _fallback(q)
    try:
        out = agent._call_claude(SYSTEM, q, model=config.AGENT_MODEL_FAST,
                                 max_tokens=200)
    except Exception as e:                                   # noqa: BLE001
        log.warning("bot 调用失败: %r", e)
        out = None
    if not out:
        return _fallback(q)
    return out.strip()[:300]
