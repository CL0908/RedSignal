# RedSignal 心动信号 — AdventureX 2026 MVP 骨架

线下轻社交系统：蓝牙匿名发现 → 双向适配 → **双方双击戒指按钮确认** → 交换预授权社交卡 → Agent 破冰。

## 快速开始

```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

打开两个浏览器窗口模拟两台手机：

- 窗口A: http://localhost:8000/?user=u_demo_a
- 窗口B: http://localhost:8000/?user=u_demo_b

## Demo 操作步骤（对应 90 秒路演）

1. 两个窗口都点 **绿**（同好交友）→ 状态变 `DISCOVERABLE`
2. 窗口A 点 **扫描附近** → 自动连续上报 3 次采样（模拟 BLE 持续发现）
3. 双方同时收到私密提醒："附近有一位与你互相适配的同好…"
4. 窗口A **双击**中间的戒指环 → 上半确认弧点亮，"等待对方…"
5. 窗口B 也双击 → 两道弧全亮，`Mutual consent confirmed.`
6. 双方看到对方社交卡（只含预授权字段）+ Agent 破冰问题
7. 随时点 **蓝** → 立即取消一切未完成的候选与确认

无 `ANTHROPIC_API_KEY` 时 Agent 自动使用预置文案（断网可演示）。
配置真实 Agent：`export ANTHROPIC_API_KEY=sk-ant-...` 后重启。

## 接入真实 Zilo 戒指

1. 用 nRF Connect 查出戒指的 Service/Characteristic UUID，
   填入 `client/logic/zilo_adapter.js` 顶部常量
2. Chrome 打开客户端，点页脚 **连接真实戒指**
3. 连接后后端自动下发 `0601` 开启上报；戒指**按钮双击**（帧 `0703`）
   即触发确认，与 Mock 按钮走完全相同的后端逻辑
4. 帧格式若实测含包头/校验，只需修改 `backend/zilo_protocol.py::parse_frame`
   与 `zilo_adapter.js` 的对应两行

已实测命令表（来自官方测试 App 日志）：
`0101/0102` 系统信息 · `0601` 开启上报 · `0605` IMU 批量帧 ·
`0701` 触摸双击 · `0702` 动作手势 · `0703` **按钮双击=确认** · `0603` 停止

## 测试

```bash
python -m pytest tests/ -q          # 单元/规则测试（T01-T09 + 协议）
python tests/e2e_smoke.py           # 端到端冒烟（需先起服务在 :8000）
```

## 结构

```
backend/
  config.py         全部阈值（现场调参只改这里）
  models.py         PRD 第10章数据结构
  store.py          内存存储（换 SQLite/Supabase 只改此文件）
  state_machine.py  PRD 9.4 状态机；切蓝最高优先级
  presence.py       BLE 候选持续性判断（防单次信号波动）
  matching.py       硬性门槛 + 绿色评分（确定性，LLM 不参与）
  confirm.py        双按钮确认器：30s 窗口 / 防抖 / 静默过期
  share_bundle.py   分享包交换：白名单 + 禁止字段硬剔除
  agent.py          LLM 破冰 + JSON 校验 + 3 套 fallback
  zilo_protocol.py  戒指帧解析（0703=确认）
  mock_data.py      预置演示用户
  main.py           FastAPI + 双 WebSocket（/ws/user, /ws/device）
client/
  index.html        虚拟戒指 UI（双确认弧动效）
  logic/            HardwareAdapter / MockAdapter / ZiloWebBluetoothAdapter
tests/              pytest + e2e 冒烟
```

## 设计红线（附录A，代码已强制）

- LLM 只生成文案，不决定匹配与交换（matching/share_bundle 纯确定性）
- 切蓝在任何状态立即生效并取消候选（state_machine.go_blue）
- 单方确认静默过期，UI 永远只显示"未建立连接"
- 禁止字段（手机号/真实姓名/精确位置/健康/原始音频）无条件剔除
- Mock 与真实硬件事件同一数据结构、同一处理入口
- 无心率相关任何代码与文案

---

## 推荐算法（backend/matching.py + tags.py）

### 两个分数，职责分离

```
compat_score  兼容分 0-100   PRD 加权公式，只决定"够不够格"（阈值 80）
rank_score    排序分         = compat + 停留奖励，只决定"先推谁"
```

停留久不代表更合适，只代表更值得优先打扰。混在一起会让阈值失去意义。

```
compat = 40×兴趣重合 + 25×目标兼容 + 20×沟通方式 + 15×场景相关
rank   = compat + min(10, 停留秒数/120 × 10)
```

### 兴趣重合的两层语义处理

1. **同义词归一化**：`AI Agent` / `人工智能代理` / `智能体` → `ai-agent`
2. **领域部分分**：`ai-agent` 与 `llm` 字面不重合，但同属 `ai` 领域，
   给 0.4 个重合credit——做 Agent 的和做大模型的显然该被撮合

两层都是纯字典查找，确定性、可复现，匹配路径不含 LLM（附录A规则6）。
LLM 只在**录入阶段**把自然语言转成标签，不参与打分。

### 两层冷却（缺一不可）

| 层 | 作用 | 默认值 |
|---|---|---|
| 按用户 | 收到提醒后静默，防止密集场景连续轰炸打断对话 | 10 分钟 |
| 按人对 | 同一对推过没成，整场活动不再重复 | 2 小时 |

10 分钟这个数字来自 MIT Serendipity（2005）的用户研究结论。

### 排序而非队列

每次扫描上报都**重新计算当下最优候选**，不维护静态队列——
舞池里人流几秒就换一批，队列会立刻过期。最优候选正忙时自动顺延下一个。

### 稳定性

因为 `compat_score(A,B) == compat_score(B,A)`（对称），按分数降序贪心选边
产生的就是**唯一稳定匹配**，不会出现两人互为最优却被拆开的情况。
因此不需要跑完整 Gale-Shapley。多人同时在场时用 `matching.stable_round()`。

### 阈值怎么定的

```bash
python tools/tune_threshold.py
```

输出标注数据集的分数分布 + 各阈值的召回/精确率 + 推荐值。
现场把真实用户标签填进 `PEOPLE` 重跑，30 秒重新标定。
