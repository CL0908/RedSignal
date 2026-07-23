# RedSignal 破冰官（Photon Spectrum）

匹配成功后，让一个**有手机号的 Agent** 主动给刚对上的双方发 **iMessage** —— 消息即界面，对方不用下载 App、不用注册，戴戒指双击确认后手机直接收到破冰消息。

贴合 AdventureX **#24 Photon「消息即界面」赛道**的「主动式服务 Agent」方向。

## 架构

```
Python 后端(匹配确认) ──HTTP /icebreak──> 本 Node 服务(spectrum-ts) ──iMessage──> 双方手机
```
> Photon 发消息只有 spectrum-ts(TypeScript) SDK，无 REST 发送接口，所以需要这层 Node。

## 两种模式

- **Mock（默认，零依赖）**：未设 `PROJECT_ID/SECRET` → 只打印「会发给谁、发什么」，无需 npm install、无需 Photon 账号，**现在就能演示整条链路**。
- **真实**：设了凭证 → 加载 spectrum-ts，真发 iMessage。

## 上真机 3 步

1. 去 Photon Dashboard 注册，用赛道 **Promo Code** 领一个月 Pro + 一个 iMessage line，拿到 `projectId` / `projectSecret`。
2. 装依赖并配置：
   ```bash
   cd photon-agent
   npm install
   export PROJECT_ID=...  PROJECT_SECRET=...
   export PHOTON_PROVIDER=imessage      # 本地调试可用 terminal
   npm start                             # 监听 :8787
   ```
3. 后端指向它（默认已是 localhost:8787，可用 `PHOTON_AGENT_URL` 覆盖），触发：
   ```bash
   curl -X POST http://localhost:8000/api/demo/u_demo_01/icebreak \
     -H 'content-type: application/json' \
     -d '{"phones":["+1你的号","+1对方号"],"shared_interests":["AI Agent","摄影"]}'
   ```
   双方手机即收到破冰官的 iMessage。

## 接口

`POST /icebreak`  body: `{ "recipients": ["+1...","+1..."], "text": "...", "group": true }`
`GET /health` → `{ ok, real, provider }`

破冰文案由后端 `backend/photon.py` 调 Claude（`agent.generate`，带断网 fallback）生成，本服务只负责投递。
