"""阈值与权重调参脚本。

用途：回答"为什么阈值定在 80 分"——不是拍脑袋，是让分数分布把
"应该匹配"和"不该匹配"两类清晰分开后选的分界点。

运行：python tools/tune_threshold.py
输出：分数分布直方图 + 各阈值下的准确率 + 推荐阈值
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, matching                      # noqa: E402
from backend.models import Mode, UserEventProfile         # noqa: E402

EV = config.DEFAULT_EVENT_ID


def p(uid, goal, tags_, style="deep_small_group"):
    return UserEventProfile(
        user_id=uid, event_id=EV, mode=Mode.FRIEND, social_goal=goal,
        interest_tags=tags_, communication_style=style,
        share_bundle={}, nickname=uid,
    )


# ---- 标注数据集：人工判断"这两个人该不该被撮合" ----
PEOPLE = {
    # AI 硬件组
    "ai1": p("ai1", "project_teammate", ["AI Agent", "智能硬件", "科幻电影"]),
    "ai2": p("ai2", "project_teammate", ["人工智能代理", "可穿戴设备", "赛博朋克"]),
    "ai3": p("ai3", "industry_chat", ["LLM", "大模型", "创业"]),
    # 电音组
    "mu1": p("mu1", "hobby_friend", ["电子音乐", "蹦迪", "摄影"], "casual_large_group"),
    "mu2": p("mu2", "hobby_friend", ["techno", "clubbing", "拍照"], "casual_large_group"),
    "mu3": p("mu3", "event_buddy", ["EDM", "电音", "旅行"], "casual_large_group"),
    # 安静阅读组
    "rd1": p("rd1", "long_term_friend", ["读书", "电影", "咖啡"], "one_on_one"),
    "rd2": p("rd2", "long_term_friend", ["阅读", "看片", "手冲"], "one_on_one"),
    # 完全不搭的
    "od1": p("od1", "event_buddy", ["健身", "攀岩", "跑步"], "casual_large_group"),
    "od2": p("od2", "project_teammate", ["设计", "UI", "摄影"]),
}

SHOULD_MATCH = [
    ("ai1", "ai2"),   # 同义词不同写法的 AI 硬件同好
    ("mu1", "mu2"),   # 电音同好
    ("rd1", "rd2"),   # 安静阅读同好
    ("ai1", "ai3"),   # AI 方向，目标略不同但相关
    ("mu1", "mu3"),   # 电音，目标略不同
]

SHOULD_NOT_MATCH = [
    ("ai1", "mu1"),   # 领域完全不同
    ("ai1", "rd1"),
    ("ai1", "od1"),
    ("mu1", "rd1"),
    ("mu1", "od2"),
    ("rd1", "od1"),
    ("ai2", "od1"),
    ("mu2", "rd2"),
    ("rd2", "od2"),
    ("ai3", "mu3"),
]


def bar(n, width=40, ch="█"):
    return ch * min(n, width)


def main() -> None:
    pos = [(a, b, matching.compat_score(PEOPLE[a], PEOPLE[b])) for a, b in SHOULD_MATCH]
    neg = [(a, b, matching.compat_score(PEOPLE[a], PEOPLE[b])) for a, b in SHOULD_NOT_MATCH]

    print("=" * 58)
    print("应该匹配的人对")
    print("=" * 58)
    for a, b, s in sorted(pos, key=lambda x: -x[2]):
        print(f"  {a:>4} × {b:<4} {s:>3}分  {bar(s // 2)}")

    print()
    print("=" * 58)
    print("不该匹配的人对")
    print("=" * 58)
    for a, b, s in sorted(neg, key=lambda x: -x[2]):
        print(f"  {a:>4} × {b:<4} {s:>3}分  {bar(s // 2)}")

    pos_scores = [s for _, _, s in pos]
    neg_scores = [s for _, _, s in neg]
    print()
    print("=" * 58)
    print(f"应匹配组：最低 {min(pos_scores)}  最高 {max(pos_scores)}  "
          f"均值 {sum(pos_scores) / len(pos_scores):.1f}")
    print(f"不该组：  最低 {min(neg_scores)}  最高 {max(neg_scores)}  "
          f"均值 {sum(neg_scores) / len(neg_scores):.1f}")
    gap = min(pos_scores) - max(neg_scores)
    if gap > 0:
        print(f"两类完全分开，间隔 {gap} 分 → 阈值可取区间 "
              f"[{max(neg_scores) + 1}, {min(pos_scores)}]")
    else:
        print(f"两类有重叠 {-gap} 分 → 无法完美分开，需按业务偏好取舍")

    print()
    print("=" * 58)
    print("各阈值表现（漏推 = 该推没推，误推 = 不该推却推了）")
    print("=" * 58)
    print(f"{'阈值':>5} {'召回':>7} {'精确':>7} {'漏推':>5} {'误推':>5}")
    best, best_f1 = None, -1.0
    for th in range(60, 101, 5):
        tp = sum(1 for s in pos_scores if s >= th)
        fn = len(pos_scores) - tp
        fp = sum(1 for s in neg_scores if s >= th)
        recall = tp / len(pos_scores)
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        mark = ""
        if f1 > best_f1:
            best_f1, best = f1, th
        if th == config.MATCH_SCORE_THRESHOLD:
            mark = "  ← 当前配置"
        print(f"{th:>5} {recall:>7.0%} {precision:>7.0%} {fn:>5} {fp:>5}{mark}")

    print()
    print(f"推荐阈值：{best}（F1 = {best_f1:.2f}）")
    if best != config.MATCH_SCORE_THRESHOLD:
        print(f"当前配置为 {config.MATCH_SCORE_THRESHOLD}，"
              f"如需调整请改 backend/config.py::MATCH_SCORE_THRESHOLD")
    else:
        print("当前配置已是最优。")

    print()
    print("提示：现场可把真实用户的标签填进 PEOPLE 重跑，30 秒重新标定阈值。")


if __name__ == "__main__":
    main()
