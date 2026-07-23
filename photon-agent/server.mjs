// RedSignal 破冰官微服务 —— 用 Photon Spectrum 主动给「刚匹配上的双方」发 iMessage。
//
// 两种模式（自动判断）：
//   • 真实：设了 PROJECT_ID + PROJECT_SECRET → 动态加载 spectrum-ts，真发 iMessage。
//   • Mock：没设凭证 → 不加载任何依赖，只把「会发给谁、发什么」打印出来。
//     ——所以无需 npm install、无需 Photon 账号，就能跑通整条链路做演示。
//
// 接口：POST /icebreak  { "recipients": ["+1555...", "+1666..."], "text": "破冰文案", "group": true }
// 由 Python 后端在「双方双击戒指确认」后调用。
//
// 上真机三步：
//   1) 去 Photon Dashboard 注册、用赛道 Promo Code 拿一个月 Pro + 一个 iMessage line
//   2) 拿到 projectId / projectSecret，export PROJECT_ID=... PROJECT_SECRET=...
//   3) npm install && npm start
import http from "node:http";

const PORT = process.env.PHOTON_AGENT_PORT || 8787;
const PROJECT_ID = process.env.PROJECT_ID || "";
const PROJECT_SECRET = process.env.PROJECT_SECRET || "";
const PROVIDER = process.env.PHOTON_PROVIDER || "imessage"; // imessage | terminal
const REAL = Boolean(PROJECT_ID && PROJECT_SECRET);

let sendImpl = null; // 真实模式下注入

async function initReal() {
  // 仅在有凭证时才加载 spectrum-ts（Mock 模式零依赖）
  const { Spectrum } = await import("spectrum-ts");
  const providers = await import("spectrum-ts/providers");
  const prov = PROVIDER === "terminal" ? providers.terminal : providers.imessage;
  const app = await Spectrum({
    projectId: PROJECT_ID,
    projectSecret: PROJECT_SECRET,
    providers: [prov.config()],
  });
  const im = prov(app);

  sendImpl = async (recipients, text, group) => {
    const users = [];
    for (const p of recipients) users.push(await im.user(p));
    if (group && users.length > 1) {
      // 建一个群把双方都拉进来，破冰官在群里开场
      const space = await im.space.create(...users);
      await space.send(text);
      return { mode: "group", sentTo: recipients };
    }
    // 否则各发一条 DM
    for (const u of users) {
      const space = await im.space.create(u);
      await space.send(text);
    }
    return { mode: "dm", sentTo: recipients };
  };
  console.log(`[photon-agent] 真实模式 (${PROVIDER})，projectId=${PROJECT_ID.slice(0, 6)}…`);
}

function mockSend(recipients, text, group) {
  console.log("─".repeat(60));
  console.log(`[photon-agent · MOCK] ${group ? "群聊破冰" : "分别私信"} → ${recipients.join(", ")}`);
  console.log(`[photon-agent · MOCK] 文案: ${text}`);
  console.log("─".repeat(60));
  return { mode: group ? "group" : "dm", sentTo: recipients, mock: true };
}

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(JSON.stringify({ ok: true, real: REAL, provider: REAL ? PROVIDER : "mock" }));
  }
  if (req.method === "POST" && req.url === "/icebreak") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      try {
        const { recipients = [], text = "", group = true } = JSON.parse(body || "{}");
        if (!recipients.length || !text) {
          res.writeHead(400); return res.end(JSON.stringify({ error: "need recipients[] + text" }));
        }
        const result = REAL ? await sendImpl(recipients, text, group) : mockSend(recipients, text, group);
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: true, ...result }));
      } catch (e) {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: String(e) }));
      }
    });
    return;
  }
  res.writeHead(404); res.end("not found");
});

(async () => {
  if (REAL) {
    try { await initReal(); }
    catch (e) { console.error("[photon-agent] 真实模式初始化失败，退回 Mock:", String(e)); }
  } else {
    console.log("[photon-agent] Mock 模式（未设 PROJECT_ID/SECRET）——只打印不真发，可直接演示链路。");
  }
  server.listen(PORT, () => console.log(`[photon-agent] listening on :${PORT}  (POST /icebreak)`));
})();
