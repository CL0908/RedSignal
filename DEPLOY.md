# 部署：前端 Vercel + 后端 Railway

## 为什么后端不能上 Vercel

Vercel Functions 从 2026 年 6 月起原生支持 WebSocket（public beta），但三条限制
正好全打在 RedSignal 的架构上：

| Vercel 的限制 | 对 RedSignal 意味着什么 |
|---|---|
| 后续连接不保证落到同一个 Function 实例 | `store` 是模块级内存单例。A 上报 sighting 要让 B 看到，两人必须在同一实例，否则永远匹配不上 |
| 连接继承 Function 时长上限，最长 300 秒 | 「发现 → 匹配 → 双确认 → 聊天」这条链路 5 分钟断一次 |
| 跨连接持久状态需自备 Redis | `hub.user_ws` 这张 user_id → WebSocket 的表没法跨实例，匹配提醒/encounter/聊天投递全部失效 |

要真上 Vercel，得把 `store` 和 `hub` 整体搬到 Redis + 外部 pub/sub——那是重写，
不是部署配置。所以：**前端静态资源上 Vercel，后端上能跑常驻进程的地方。**

---

## 后端 · Railway

1. Railway 新建项目 → Deploy from GitHub repo → 选 `CL0908/RedSignal`
2. Railway 读 `requirements.txt` 自动识别 Python，用仓库根的 `Procfile` 启动
3. Variables 里配环境变量（对应 `.env.example`）：

```
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role>
SUPABASE_ANON_KEY=<anon public>
SUPABASE_JWT_SECRET=<JWT Secret，非对称密钥项目留空>
REDSIGNAL_ALLOWED_ORIGINS=https://<你的>.vercel.app
REDSIGNAL_DEMO_MODE=1
ANTHROPIC_API_KEY=<可选，不配走确定性 fallback>
```

`REDSIGNAL_ALLOWED_ORIGINS` 必须填 Vercel 的域名，否则前端所有 REST 请求被 CORS 拦。
（WebSocket 不走 CORS，但 REST 全线挂，表现为"能连上但什么数据都没有"。）

4. Railway 会给一个 `https://xxx.up.railway.app`，记下来给前端用。

**演示结束后把 `REDSIGNAL_DEMO_MODE` 设成 0**：开着的话，`mock_data` 里的
预置用户（`u_demo_a`/`d01`…）不带 token 就能连。这是给「舞池密集场景」演示留的口子，
不是给公网留的。

---

## 前端 · Vercel

前端是纯静态的（`client/app/`），不需要构建。

1. Vercel 新建项目 → 同一个 GitHub repo
2. **Root Directory 设成 `client/app`**，Framework Preset 选 `Other`，
   Build Command 留空，Output Directory 留空
3. 部署前把 `client/app/index.html` 和 `client/app/login.html` 里的

   ```html
   <meta name="rs-api-base" content="">
   ```

   改成 Railway 的地址：

   ```html
   <meta name="rs-api-base" content="https://xxx.up.railway.app">
   ```

留空表示「后端与前端同源」，只适用于本地或后端自己托管前端的情况。

---

## Supabase 侧

1. SQL Editor 跑一遍 `docs/supabase_schema.sql`（可重复执行，末尾有
   `alter table ... add column if not exists` 给已建表的项目补列）
2. Authentication → Providers → Email 打开
3. Authentication → Providers → Email → **Confirm email 关掉**，
   否则注册后要先收邮件才能登录，而 Supabase 内置邮件服务限流很低
   （要保留邮箱确认，就得先配自己的 SMTP）
4. Project Settings → API 抄三个值到 Railway 的环境变量

---

## 本地跑

```bash
pip install -r requirements.txt
cp .env.example .env      # 填上 Supabase 的三个 key
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

后端会同时托管前端：`http://localhost:8000/app/`
（此时 `rs-api-base` 留空即可，同源。）

两台手机连同一局域网，开 `http://<你的内网IP>:8000/app/` 即可对演。
