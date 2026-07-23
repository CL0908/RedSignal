"""RedSignal 可配置参数。全部阈值集中在此，比赛现场按需调整。"""

# ---- 匹配 ----
MATCH_SCORE_THRESHOLD = 80        # 适配分及格线（0-100），仅做准入，不决定推送顺序
CANDIDATE_TTL_SECONDS = 300       # 候选 Pair 有效期

# ---- 冷却（两层，缺一不可）----
# 按用户：收到一次提醒后静默期。防止密集场景（舞池）连续轰炸打断对话。
# 数值参考 MIT Serendipity 用户研究结论：每 10 分钟最多一次介绍。
USER_NOTIFY_COOLDOWN_SECONDS = 600
# 按人对：同一对推过但没成，整场活动内不再重复推荐。
PAIR_RETRY_COOLDOWN_SECONDS = 7200

# ---- BLE 候选持续性 ----
PRESENCE_MIN_SIGHTINGS = 3        # 候选必须连续出现的采样次数
PRESENCE_WINDOW_SECONDS = 30      # 采样窗口
RSSI_VERY_NEAR = -55              # RSSI 分段（近似，不承诺米数）
RSSI_NEAR = -75

# ---- 排序信号：共同停留时长 ----
# 密集场景下所有人都是 very_near，距离维度失效，用停留时长区分
# "擦肩而过" 与 "在你旁边站了两分钟"。仅影响排序，不影响是否合格。
DWELL_BONUS_MAX = 10.0            # 停留奖励上限（分）
DWELL_SATURATE_SECONDS = 120.0    # 停留多久拿满奖励

# ---- 双按钮确认 ----
CONFIRM_WINDOW_SECONDS = 30       # 双确认时间窗
BUTTON_DEBOUNCE_SECONDS = 3       # 同一用户同一 pair 的按键防抖（双击事件）

# ---- Agent ----
AGENT_TIMEOUT_SECONDS = 5
AGENT_MODEL = "claude-sonnet-4-6"

# ---- IMU ----
IMU_MEMORY_SECONDS = 10           # 内存中保留的原始 IMU 批次时长（不入库）

# ---- 活动 ----
DEFAULT_EVENT_ID = "adventurex_2026"
