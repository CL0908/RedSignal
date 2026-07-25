/* RedSignal 手机端 App ←→ 后端接线层。
 *
 * 设计原则：index.html 保持"能离线打开的纯前端"。本文件在它之后加载，
 * 只做两件事——
 *   1) 包裹已有的全局函数（selectMode / startDiscovery / ...），
 *      在原有 UI 行为之后追加一次后端调用；
 *   2) 订阅 /ws/user/{uid} 的推送，把后端事件回灌进 UI。
 * 后端连不上时全部静默降级，界面仍是原来的可演示 Mock 状态。
 *
 * 用法：http://localhost:8000/app?user=u_demo_a
 */
(function () {
  'use strict';

  const params = new URLSearchParams(location.search);
  /* 后端地址。同机部署时就是当前源；前端上 Vercel、后端上 Railway 时是两个域，
     用 <meta name="rs-api-base" content="https://xxx.up.railway.app"> 指过去。
     跨域时后端必须配 REDSIGNAL_ALLOWED_ORIGINS，否则 REST 全被 CORS 拦。 */
  // 本机跑的时候一律同源：meta 里填的是线上 Railway 域名，本地页面照着打会被
  // CORS 拦掉，而 WebSocket 不走 CORS 仍显示「已连接」，排查起来很误导。
  const isLocal = /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])$/.test(location.hostname)
                  || /^192\.168\.|^10\.|^172\.(1[6-9]|2\d|3[01])\./.test(location.hostname);
  const HTTP = isLocal ? location.origin
    : ((document.querySelector('meta[name="rs-api-base"]')?.content || '').trim()
       || location.origin);
  const WS = HTTP.replace(/^http/, 'ws');
  const SESSION_KEY = 'redsignal.session';

  /* 身份来源有两条，优先级从高到低：
       1) Supabase 登录会话（localStorage）——真实用户，user_id 是 auth uid，
          后端会用 token 的 sub 复核，前端说了不算；
       2) ?user=u_demo_a 这种预置演示用户——只有后端演示模式开着才放行。
     两条都没有就跳登录页。 */
  function loadSession() {
    try {
      const s = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null');
      if (s && s.access_token && s.expires_at * 1000 > Date.now()) return s;
      if (s) localStorage.removeItem(SESSION_KEY);   // 过期的清掉
    } catch { /* 坏数据当没登录 */ }
    return null;
  }

  const session = loadSession();
  const demoUser = params.get('user');
  const USER = session ? session.user_id : (demoUser || '');
  const TOKEN = session ? session.access_token : '';

  /* 两套体验，同一份代码：
       judge  —— 评委版（loginpre.html 进入）。感应 → 1~2 秒 → 确认成功 →
                 跳状态页。台上那 30 秒不允许开天窗，所以不赌真实匹配。
       public —— 外部用户（扫码进入）。走真实标签匹配，不演任何东西；
                 手机没有 Web Bluetooth，戒指相关的入口一律不出现。 */
  const MODE = localStorage.getItem('redsignal.mode') === 'judge' ? 'judge' : 'public';
  const IS_JUDGE = MODE === 'judge';

  if (!USER) {
    location.replace('login.html');
    return;
  }

  function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    if (TOKEN) h['Authorization'] = 'Bearer ' + TOKEN;
    return h;
  }

  function logout() {
    localStorage.removeItem(SESSION_KEY);
    location.replace('login.html');
  }
  window.rsLogout = logout;

  // 红/绿/蓝 ←→ 后端 Mode 枚举（models.py::Mode）
  const MODE_TO_API = { red: 'love', green: 'friend', blue: 'off' };
  const API_TO_MODE = { love: 'red', friend: 'green', off: 'blue' };

  const RS = {
    userId: USER,
    ws: null,
    online: false,
    state: 'BLUE_OFFLINE',
    mode: 'blue',
    pairId: null,
    selfConfirmed: false,
    ephemerals: [],
    scanTimer: null,
    demoMatchTimer: null,
    applying: false,   // true = 正在把后端状态回灌 UI，此时不要再往回发 set_mode
    token: '',         // ring.js 连 /ws/device 时要带
    // 会话按昵称索引（index.html 的 conversations 就是这么存的），
    // 但后端路由用 encounter_id——前端始终不需要知道对方 user_id。
    encounterByName: {},
    analyzed: {},      // encounter_id -> true，避免同一段聊天反复送去学偏好
  };
  RS.token = TOKEN;
  window.RS = RS;

  // ---------------------------------------------------------------- 工具
  function send(obj) {
    if (RS.ws && RS.ws.readyState === WebSocket.OPEN) {
      RS.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  async function api(path, opts) {
    const o = Object.assign({}, opts);
    o.headers = authHeaders(o.headers);
    const r = await fetch(HTTP + path, o);
    if (r.status === 401) { logout(); throw new Error('unauthenticated'); }
    if (!r.ok) throw new Error(path + ' -> ' + r.status);
    return r.json();
  }

  function jsonPatch(path, body) {
    return api(path, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }).catch(() => null);
  }

  // ------------------------------------------------------ 断线重连提示
  function showReconnect(on) {
    let el = document.getElementById('rs-reconnect');
    if (!el) {
      if (!on) return;
      el = document.createElement('div');
      el.id = 'rs-reconnect';
      el.style.cssText = [
        'position:absolute', 'top:0', 'left:0', 'right:0', 'z-index:60',
        'background:#2b2621', 'color:#f6f1e7', 'text-align:center',
        'padding:7px 12px', 'font-size:12px', 'letter-spacing:.02em',
      ].join(';');
      el.textContent = '连接断开，正在重连…';
      document.getElementById('app')?.appendChild(el);
    }
    el.style.display = on ? 'block' : 'none';
  }

  // ------------------------------------------------------- 顶栏连接状态
  function setRingStatus(text, ok) {
    const el = document.querySelector('.ring-status');
    if (!el) return;
    el.childNodes[el.childNodes.length - 1].nodeValue = text;
    const dot = el.querySelector('.ring-dot');
    if (dot) dot.style.background = ok ? '' : '#c8c2b6';
  }

  // ------------------------------------------------------------ 匹配横幅
  function ensureBanner() {
    let b = document.getElementById('rs-banner');
    if (b) return b;
    b = document.createElement('div');
    b.id = 'rs-banner';
    b.style.cssText = [
      'position:absolute', 'left:16px', 'right:16px', 'bottom:86px', 'z-index:40',
      'background:#2b2621', 'color:#f6f1e7', 'border-radius:16px',
      'padding:14px 16px', 'font-size:13px', 'line-height:1.55',
      'box-shadow:0 10px 30px rgba(0,0,0,.28)', 'display:none',
    ].join(';');
    b.innerHTML =
      '<div id="rs-banner-text" style="margin-bottom:10px"></div>' +
      '<div style="display:flex;gap:8px">' +
      '  <button id="rs-banner-confirm" style="flex:1;border:0;border-radius:10px;' +
      'padding:9px 0;font-size:13px;font-weight:600;background:#f6f1e7;color:#2b2621">' +
      '按下戒指确认</button>' +
      '  <button id="rs-banner-close" style="border:0;border-radius:10px;padding:9px 14px;' +
      'font-size:13px;background:rgba(246,241,231,.16);color:#f6f1e7">忽略</button>' +
      '</div>';
    document.getElementById('app').appendChild(b);
    b.querySelector('#rs-banner-confirm').onclick = () => {
      send({ action: 'app_confirm' });
      b.querySelector('#rs-banner-confirm').textContent = '已确认，等待对方…';
      b.querySelector('#rs-banner-confirm').disabled = true;
    };
    b.querySelector('#rs-banner-close').onclick = () => hideBanner();
    return b;
  }

  function showBanner(text, withConfirm) {
    const b = ensureBanner();
    b.querySelector('#rs-banner-text').textContent = text;
    const btn = b.querySelector('#rs-banner-confirm');
    btn.style.display = withConfirm ? '' : 'none';
    btn.disabled = false;
    btn.textContent = '按下戒指确认';
    b.style.display = 'block';
  }

  function hideBanner() {
    const b = document.getElementById('rs-banner');
    if (b) b.style.display = 'none';
  }

  // ------------------------------------------------------------ 消息列表
  function upsertConversation(name, preview, systemLine, lines) {
    const list = document.querySelector('.msg-list');
    const convs = getConversations();
    if (!list || !convs) return;
    if (!convs[name]) convs[name] = [];
    const conv = convs[name];
    if (systemLine && !conv.some(m => m.type === 'system' && m.text === systemLine)) {
      conv.unshift({ type: 'system', text: systemLine });
    }
    (lines || []).forEach(t => {
      if (!conv.some(m => m.text === t)) {
        conv.push({ type: 'incoming', text: t, time: nowHHMM() });
      }
    });

    let item = [...list.querySelectorAll('.msg-item')]
      .find(el => el.querySelector('.msg-name')?.textContent === name);
    if (!item) {
      item = document.createElement('div');
      item.className = 'msg-item';
      item.onclick = () => window.openChat(name);
      item.innerHTML =
        '<div class="msg-avatar"><svg viewBox="0 0 24 24" fill="none" stroke="#34C759" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" ' +
        'cy="12" r="10"/><circle cx="9" cy="10" r="1.5" fill="#34C759"/><circle cx="15" ' +
        'cy="10" r="1.5" fill="#34C759"/></svg></div>' +
        '<div class="msg-body"><div class="msg-top"><span class="msg-name"></span>' +
        '<span class="msg-time"></span></div><div class="msg-preview"></div></div>';
      item.querySelector('.msg-name').textContent = name;
      list.prepend(item);
    }
    item.querySelector('.msg-preview').textContent = preview;
    item.querySelector('.msg-time').textContent = nowHHMM();
  }

  /* index.html 里的 tags / conversations / currentWish 是顶层 `let`——
     那是全局词法绑定，不是 window 的属性，只能用裸标识符访问。 */
  const getConversations = () =>
    (typeof conversations !== 'undefined' ? conversations : null);
  const getTags = () => (typeof tags !== 'undefined' ? tags : null);

  function nowHHMM() {
    return new Date().toLocaleTimeString('zh-CN',
      { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  function hhmm(iso) {
    const d = new Date(iso);
    return isNaN(d) ? nowHHMM()
      : d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  const activeChat = () =>
    (typeof activeChatContact !== 'undefined' ? activeChatContact : '');

  function isChatOpenWith(name) {
    const v = document.getElementById('chat-view');
    return !!v && v.classList.contains('open') && activeChat() === name;
  }

  // -------------------------------------------------------- WebSocket 主通道
  function connect() {
    // token 走 query：WebSocket 握手没法带自定义头，这是浏览器 API 的限制。
    // 后端只认 token 的 sub，URL 里的 user_id 仅作声明。
    const q = TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : '';
    RS.ws = new WebSocket(`${WS}/ws/user/${encodeURIComponent(USER)}${q}`);

    RS.ws.onopen = () => {
      RS.online = true;
      showReconnect(false);
      // 页面上当前选中的模式即时同步给后端
      const sel = document.querySelector('.mode-card.selected')?.dataset.mode;
      if (sel) send({ action: 'set_mode', mode: MODE_TO_API[sel] });
    };

    RS.ws.onclose = (e) => {
      RS.online = false;
      if (e.code === 4401 || e.code === 4403) {
        // 4401 = token 过期/伪造；4403 = URL 身份与 token 不符（客户端 bug）
        logout();
        return;
      }
      setRingStatus('', false);
      // 服务重启会一次性踢掉所有连接（实测遇到过 1012 service restart）。
      // 原来是静默重连，用户只会觉得"点了没反应"，得给个可见提示。
      showReconnect(true);
      setTimeout(connect, 2000);
    };

    RS.ws.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      handle(m);
    };
  }

  function handle(m) {
    switch (m.type) {
      case 'state': {
        RS.state = m.state;
        RS.mode = API_TO_MODE[m.mode] || 'blue';
        const card = document.querySelector(`.mode-card[data-mode="${RS.mode}"]`);
        if (card && !card.classList.contains('selected')) {
          // 后端权威状态回灌 UI。必须屏蔽回声：否则这次回灌又发一条 set_mode，
          // 和用户刚点的模式互相盖写，模式会在两个值之间反复横跳。
          RS.applying = true;
          try { window.selectMode(RS.mode); } finally { RS.applying = false; }
        }
        break;
      }
      case 'sighting_ack':
        // reason 是内部诊断值（blue_mode / need_more_sightings / cooldown…），
        // 直接贴到界面上会漏出英文技术词。只在 console 留痕，不给用户看。
        if (m.reason && m.reason !== 'matched') console.debug('[RS] sighting:', m.reason);
        break;
      case 'match_notice':
        RS.pairId = m.pair_id;
        RS.selfConfirmed = false;
        // 体验流正在跑时不要抢它的文案——两边都写 radar-title 会互相盖，
        // 现场看到的就是"匹配确认成功"闪一下又变回"匹配成功"。
        if (!RS.flowRunning) {
          const t = document.getElementById('radar-title');
          if (t) t.textContent = '匹配成功';
          showBanner(`${m.text}（适配 ${m.match_score} · ${m.proximity_band}）`, true);
          if (typeof window.playSayHiOnce === 'function') window.playSayHiOnce();
        }
        break;
      case 'self_confirmed':
        RS.selfConfirmed = true;
        showBanner(m.text, false);
        break;
      case 'no_connection':
        RS.pairId = null;
        RS.selfConfirmed = false;
        showBanner('未建立连接。', false);
        setTimeout(hideBanner, 2500);
        break;
      case 'encounter': {
        hideBanner();
        if (!RS.flowRunning) {
          const t = document.getElementById('radar-title');
          if (t) t.textContent = '连接成功';
        }
        const card = m.card || {};
        const name = card.nickname || '新连接';
        const interests = (card.shared_interests || []).join(' · ');
        upsertConversation(name, '刚刚建立连接',
          `你们通过双方确认建立了匿名连接${interests ? ' · ' + interests : ''}`, []);
        if (typeof window.toast === 'function') window.toast(`已与 ${name} 建立连接`);
        RS.lastPartner = name;
        RS.encounterByName[name] = m.encounter_id;   // 聊天路由键
        break;
      }
      case 'chat_message': {
        // 对方发来的消息。encounter_id 反查昵称，落进对应会话。
        const name = Object.keys(RS.encounterByName)
          .find(n => RS.encounterByName[n] === m.encounter_id) || RS.lastPartner;
        if (!name) break;
        const convs = getConversations();
        if (convs) {
          (convs[name] = convs[name] || []).push({
            type: 'incoming', text: m.text, time: nowHHMM(),
          });
        }
        upsertConversation(name, m.text, null, []);
        // 正开着这个人的聊天窗就立刻重绘
        if (isChatOpenWith(name) && typeof window.renderConversation === 'function') {
          window.renderConversation();
        }
        break;
      }
      case 'chat_error':
        if (typeof window.toast === 'function') window.toast(m.text || '消息发送失败');
        break;
      case 'agent_content': {
        // agent.py 契约：connection_reason / icebreaker / memory_caption
        // （_REQUIRED_KEYS 强校验，fallback 也是这三个 key）
        const name = RS.lastPartner || '新连接';
        const lines = [m.connection_reason, m.icebreaker].filter(Boolean);
        upsertConversation(name, m.icebreaker || '破冰官已就位',
                           m.memory_caption || null, lines);
        break;
      }
      case 'watch_update':
        applyWatch(m.data || {});
        break;
      case 'ring_audio':
        if (typeof window.toast === 'function') {
          if (m.stage === 'completed') {
            window.toast(`戒指录音已收到（${m.size || 0} 字节），等待转写`);
          } else if (m.stage === 'transcribed') {
            const el = document.getElementById('wish-text');
            if (el && m.text) el.innerHTML = m.text +
              '<span class="muted"> · 戒指语音已更新</span>';
            if (typeof window.toast === 'function') window.toast('已把戒指语音设为今天想找的目标');
          } else if (m.stage === 'error') {
            window.toast(`戒指录音提取失败（错误码 ${m.errorCode ?? '未知'}）`);
          }
        }
        break;
      case 'device_offline':
        setRingStatus('戒指已断开', false);
        break;
    }
  }

  // ------------------------------------------------------------ 手环数据
  function applyWatch(w) {
    const stats = document.querySelectorAll('.card-health .stat .stat-value');
    if (!stats.length) return;
    if (w.today_steps != null) {
      stats[0].textContent = w.today_steps;
      // 距离按 0.72m/步估算，只做展示（不进匹配，设计红线）
      stats[1].innerHTML = (w.today_steps * 0.00072).toFixed(1) + '<small>km</small>';
    }
    if (w.heart_rate_bpm != null && stats[2]) {
      stats[2].innerHTML = w.heart_rate_bpm + '<small>bpm</small>';
      const lbl = stats[2].parentElement.querySelector('.stat-label');
      if (lbl) lbl.textContent = '心率';
    }
  }

  let seededDemo = false;

  async function refreshDevices() {
    try {
      const d = await api(`/api/devices/${USER}`);
      const w = d.watch || {};
      // 没接真手表时注入一组演示生理数据，否则「今天」卡和状态页都是空的。
      // 只注一次，真手表一旦上报就不再覆盖。
      if (!seededDemo && !w.connected && !(w.today_steps > 0)) {
        seededDemo = true;
        await api(`/api/demo/${USER}/mock`, { method: 'POST' }).catch(() => null);
        return refreshDevices();
      }
      applyWatch(w);
      if (d.ring && d.ring.connected) {
        setRingStatus('戒指已连接', true);
      }
    } catch { /* 后端未起，保持 Mock 展示 */ }
  }

  // ------------------------------------------------------------ 档案同步
  async function loadProfile() {
    try {
      const p = await api(`/api/profile/${USER}`);
      if (p.error) return;
      if (Array.isArray(p.interest_tags) && p.interest_tags.length && getTags()) {
        tags.length = 0;
        p.interest_tags.forEach(t => tags.push(t));
        window.renderTags();
      }
      if (p.wish) {
        if (typeof currentWish !== 'undefined') currentWish = p.wish;
        const el = document.getElementById('wish-text');
        if (el) el.innerHTML = p.wish +
          '<span class="muted"> · 长按戒指说一句话可以改</span>';
      }
      if (p.nickname) {
        const who = document.querySelector('.logo');
        if (who) who.title = `${p.nickname} · ${session?.email || USER}`;
      }
      // 新用户档案是空壳（ensure_profile 建的），提示去填标签，
      // 否则算不出兴趣重合分，永远进不了候选
      if (session && (!p.interest_tags || !p.interest_tags.length)) {
        setTimeout(() => {
          if (typeof window.toast === 'function') {
            window.toast('先在「我的标签」里加几个标签，才能被匹配到');
          }
        }, 1200);
      }
    } catch { /* 静默 */ }
  }

  /* 标签与「今天想找」本来就是改一次存一次，但界面上没有任何反馈，
     用户不知道存没存，会怀疑白填了。给一个短暂的「已保存」。 */
  function savedHint() {
    let el = document.getElementById('rs-saved');
    if (!el) {
      el = document.createElement('div');
      el.id = 'rs-saved';
      el.style.cssText = [
        'position:absolute', 'left:50%', 'transform:translateX(-50%)',
        'bottom:96px', 'z-index:45', 'background:rgba(44,38,32,.86)',
        'color:#f6f1e7', 'padding:7px 16px', 'border-radius:20px',
        'font-size:12px', 'opacity:0', 'transition:opacity .2s',
        'pointer-events:none',
      ].join(';');
      el.textContent = '已自动保存';
      document.getElementById('app')?.appendChild(el);
    }
    el.style.opacity = '1';
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.style.opacity = '0'; }, 1400);
  }

  const pushTags = () => {
    const t = getTags();
    if (!t) return;
    jsonPatch(`/api/profile/${USER}`, { interest_tags: t.slice() }).then(r => {
      if (r) savedHint();
    });
  };

  // ---------------------------------------------------- 包裹已有全局函数
  function wrap(name, after) {
    const orig = window[name];
    if (typeof orig !== 'function') return;
    window[name] = function (...args) {
      const r = orig.apply(this, args);
      try { after.apply(this, args); } catch (e) { console.warn('[RS]', name, e); }
      return r;
    };
  }

  wrap('selectMode', (mode) => {
    if (RS.applying) return;               // 后端回灌，不回声
    send({ action: 'set_mode', mode: MODE_TO_API[mode] });
    if (mode === 'blue') { stopScan(); hideBanner(); }
  });

  wrap('confirmAddTag', pushTags);
  wrap('removeTag', pushTags);
  wrap('confirmWish', () => {
    if (typeof currentWish !== 'undefined') {
      jsonPatch(`/api/profile/${USER}`, { wish: currentWish })
        .then(r => { if (r) savedHint(); });
    }
  });

  /* ================= 现场体验流 =================
     现场几十上百枚戒指同时在场，认不出「哪一枚是我们的」，
     也不能指望两位评委刚好互相匹配上。所以「附近」页走这条必成流程：

       点开始发现 → 感应附近设备 → 约 2 秒 → 确认成功 → 跳状态页

     感应这一步在**桌面 Chrome 上是真的**：调 Web Bluetooth 设备选择器，
     列出的就是现场真实存在的 BLE 设备。iPhone 没有 Web Bluetooth
     （任何浏览器都没有），退化为纯计时，两边观感一致。

     真实匹配链路（sighting → 持续性判断 → 双向确认）代码原样保留，
     照常在后台跑；这里只是保证台上那 30 秒不会开天窗。 */
  const FLOW = {
    senseMs: 2000,        // 没连上戒指时，感应到确认之间的停顿
    buttonWaitMs: 6000,   // 连上戒指后等真实按键的上限，超时照样放行
    toStatusMs: 1800,     // 确认成功到跳状态页之间的停顿
  };

  function setRadar(title, sub) {
    const t = document.getElementById('radar-title');
    const s = document.getElementById('radar-sub');
    if (t) t.textContent = title;
    if (s) s.textContent = sub || '';
  }

  async function senseNearby() {
    // 优先连"已经授权过的那枚戒指"——免弹窗，评委不用点任何东西。
    // 需要 chrome://flags/#enable-web-bluetooth-new-permissions-backend，
    // 且戒指处于唤醒状态（睡着时它完全不广播，这是实测过的）。
    try {
      const name = await window.Ring?.autoConnect?.(6000);
      if (name) return name;
    } catch { /* 没开 flag / 没授权过 / 戒指睡着，都走下面 */ }
    return 'timer';        // 连不上也照常往下走：台上不能卡住
  }

  async function runFlow() {
    setRadar('正在匿名发现', '正在感应附近的戒指…');
    const via = await senseNearby();

    if (via === 'timer') {
      // 没连上戒指（没开 flag / 没授权 / 戒指睡着 / 手机端）：走计时
      setRadar('感应到附近的戒指', '正在建立匿名连接…');
      await new Promise(r => setTimeout(r, FLOW.senseMs));
    } else {
      // 连上了：等一次真实按键。按了立刻确认，没按满 6 秒也放行——
      // 台上绝不能因为"评委没按对"而卡住。
      setRadar('已感应到戒指', '按一下戒指，确认你愿意认识对方');
      const pressed = await window.Ring.waitForButton(FLOW.buttonWaitMs);
      RS.confirmedByRing = pressed;
      if (pressed) {
        setRadar('已收到你的确认', '正在建立连接…');
        await new Promise(r => setTimeout(r, 700));
      }
    }

    setRadar('匹配确认成功',
             RS.confirmedByRing ? '由你按下的戒指确认' : '双方都愿意认识对方');
    if (typeof window.playSayHiOnce === 'function') window.playSayHiOnce();
    if (typeof window.toast === 'function') {
      window.toast(RS.confirmedByRing ? '戒指确认成功' : '匹配确认成功');
    }

    await new Promise(r => setTimeout(r, FLOW.toStatusMs));
    if (typeof window.goTo === 'function') window.goTo('status');
  }

  /* 外部用户版的「附近」：列出最合得来的几个人。

     为什么是列表而不是"最近的那一位"：手机浏览器（iOS 与安卓一样）
     拿不到 BLE，没法按距离筛人，"谁离你最近"根本无从判断。
     与其假装知道，不如把最合适的几个一起摆出来让用户自己挑。 */
  async function loadRecommendations() {
    let data;
    try {
      data = await api(`/api/nearby/${USER}?limit=3`);
    } catch {
      setRadar('暂时连不上', '稍后再试一次');
      return;
    }
    const items = (data && data.items) || [];
    const box = ensureRecoBox();
    if (!items.length) {
      setRadar('还没有合适的人', '换几个更具体的标签，或稍后再看');
      box.innerHTML = '';
      return;
    }
    setRadar(`为你找到 ${items.length} 位`, '都和你的兴趣与目标对得上');
    box.innerHTML = items.map(it => {
      const shared = (it.shared_interests || []).slice(0, 3)
        .map(s => `<span style="display:inline-block;padding:2px 8px;margin:2px 4px 2px 0;
          border-radius:10px;background:rgba(44,38,32,.07);font-size:11px">${esc(s)}</span>`)
        .join('');
      return `<div style="background:var(--card);border:1.2px solid rgba(44,38,32,.12);
          border-radius:16px;padding:12px 14px;margin-bottom:10px;text-align:left">
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <strong style="font-size:15px">${esc(it.nickname || '匿名')}</strong>
            <span style="font-size:11px;color:var(--sub)">适配 ${it.match_score}</span>
          </div>
          ${it.wish ? `<div style="font-size:12px;color:var(--sub);margin-top:3px">
            今天想找：${esc(it.wish)}</div>` : ''}
          <div style="margin-top:6px">${shared}</div>
        </div>`;
    }).join('');
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function ensureRecoBox() {
    let b = document.getElementById('rs-reco');
    if (!b) {
      b = document.createElement('div');
      b.id = 'rs-reco';
      b.style.cssText = 'margin-top:18px;width:100%;max-width:340px';
      document.querySelector('.radar-status')?.after(b);
    }
    return b;
  }

  wrap('startDiscovery', () => {
    const mode = document.querySelector('.mode-card.selected')?.dataset.mode || 'red';
    if (mode === 'blue') window.selectMode('red');
    else send({ action: 'set_mode', mode: MODE_TO_API[mode] });
    startScan();
    if (IS_JUDGE) {
      // 评委版：必成流程
      RS.flowRunning = true;
      runFlow().finally(() => { RS.flowRunning = false; });
    } else {
      const t = getTags();
      if (!t || !t.length) {
        setRadar('还差一步', '先回「信号」页加几个标签，才能算出适合你的人');
      } else {
        setRadar('正在为你寻找', '按兴趣与目标匹配中…');
        loadRecommendations();
      }
    }
  });

  wrap('stopDiscovery', () => {
    stopScan();
    setRadar('正在匿名发现', '正在感应附近的戒指…');
  });

  // 匿名编号是内部标识，现场不展示（元素已 hidden，这里只维持数据）
  wrap('updateAnonCode', () => {
    const mine = RS.ephemerals.find(e => e.user_id === USER);
    const el = document.getElementById('anon-code');
    if (el && mine) el.textContent = '#' + mine.ephemeral_id.slice(-6).toUpperCase();
  });

  // 每次切到状态页都换一副新状态——停在同一组数值上像是坏了
  wrap('goTo', (page) => {
    if (page !== 'status') return;
    const f = document.getElementById('status-page-frame');
    try { f?.contentWindow?.__rsRandomize?.(); } catch { /* 还没加载完 */ }
  });

  // 点吉祥物 = Mock 戒指双击（无硬件时的确认兜底）
  wrap('onMascotClick', () => {
    if (RS.pairId && !RS.selfConfirmed) send({ action: 'mock_button' });
  });

  // ------------------------------------------------------------ 聊天收发
  /* sendChatMessage 不能用 wrap()：原函数会先清空输入框，
     等到 after 钩子跑的时候文本已经没了。所以整体替换，先取文本再放行。 */
  const BOT_NAME = '小黑';

  const origSendChat = window.sendChatMessage;
  if (typeof origSendChat === 'function') {
    window.sendChatMessage = function (event) {
      const input = document.getElementById('chat-input');
      const text = input ? input.value.trim() : '';
      const name = activeChat();
      const r = origSendChat.apply(this, arguments);   // 本地乐观渲染

      if (name === BOT_NAME) {
        if (text) askBot(text);
        return r;
      }
      const eid = RS.encounterByName[name];
      if (text && eid) send({ action: 'chat_send', encounter_id: eid, text });
      return r;
    };
  }

  /** 问小黑。回答走后端（有 Claude 就用，没有则走后端的关键词兜底）。 */
  async function askBot(question) {
    const convs = getConversations();
    let reply;
    try {
      const r = await api(`/api/bot/${USER}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ message: question }),
      });
      reply = r && r.text;
    } catch {
      reply = '我这会儿有点忙，稍后再问我一次？';
    }
    if (!reply) return;
    if (convs) {
      (convs[BOT_NAME] = convs[BOT_NAME] || []).push({
        type: 'incoming', text: reply, time: nowHHMM(),
      });
    }
    if (isChatOpenWith(BOT_NAME) && typeof window.renderConversation === 'function') {
      window.renderConversation();
    }
    const item = [...document.querySelectorAll('.msg-item')]
      .find(el => el.querySelector('.msg-name')?.textContent === BOT_NAME);
    if (item) item.querySelector('.msg-preview').textContent = reply;
  }

  // 打开会话时拉一次历史，覆盖本地乐观副本（刷新/重连后对齐）
  wrap('openChat', (name) => {
    const eid = RS.encounterByName[name];
    if (!eid) return;                       // mock 会话没有 encounter，保持原样
    api(`/api/chat/${USER}/history/${eid}`).then(h => {
      if (!h || h.error || !Array.isArray(h.messages)) return;
      const convs = getConversations();
      if (!convs) return;
      const sys = (convs[name] || []).filter(m => m.type === 'system');
      convs[name] = sys.concat(h.messages.map(m => ({
        type: m.mine ? 'outgoing' : 'incoming', text: m.text, time: hhmm(m.ts),
      })));
      if (isChatOpenWith(name) && typeof window.renderConversation === 'function') {
        window.renderConversation();
      }
    }).catch(() => { /* 拉不到就用本地副本 */ });
  });

  // 关闭会话 = 这段聊天结束 → 送去评融洽度并更新偏好（每个 encounter 只送一次，
  // 否则同一段对话会被反复计入，权重虚高）
  wrap('closeChat', () => {
    const name = activeChat();
    const eid = RS.encounterByName[name];
    if (!eid || RS.analyzed[eid]) return;
    const convs = getConversations();
    const real = (convs?.[name] || []).filter(m => m.type !== 'system');
    if (real.length < 2) return;            // 一两句话不足以学到东西
    RS.analyzed[eid] = true;
    api(`/api/chat/${USER}/analyze`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ encounter_id: eid }),
    }).then(r => {
      if (!r || r.error) { RS.analyzed[eid] = false; return; }
      RS.lastRapport = r.rapport;
      const top = (r.updated_preference_top || []).map(t => t[0]).slice(0, 2);
      if (top.length && typeof window.toast === 'function') {
        window.toast('已更新你的偏好：' + top.join(' · '));
      }
    }).catch(() => { RS.analyzed[eid] = false; });
  });

  // ------------------------------------------------------------ 扫描循环
  async function startScan() {
    try {
      RS.ephemerals = await api('/api/ephemerals');
    } catch { return; }
    window.updateAnonCode();
    stopScan();
    tick();
    // Demo 两个账号需要在现场约 3 秒内完成三次持续性观测；真实账号仍用 2 秒节奏。
    const interval = /^u_demo_/.test(RS.userId) ? 1000 : 2000;
    RS.scanTimer = setInterval(tick, interval);
    // 演示后门：3 秒后强行配对（后端会把分数抬到 80 绕过阈值）。
    // 后端 REDSIGNAL_DEMO_AUTOPLAY=0 时不发——验真戒指时它会掩盖真实故障：
    // 蓝牙那一跳就算没通，自动确认也会让界面显示"成功"。
    if (/^u_demo_[ab]$/.test(RS.userId) && RS.demoAutoplay !== false) {
      clearTimeout(RS.demoMatchTimer);
      RS.demoMatchTimer = setTimeout(() => {
        if (!RS.pairId) send({ action: 'demo_match' });
      }, 3000);
    }
  }

  function stopScan() {
    if (RS.scanTimer) clearInterval(RS.scanTimer);
    RS.scanTimer = null;
    clearTimeout(RS.demoMatchTimer);
    RS.demoMatchTimer = null;
  }

  /* 每轮只上报一小撮，而不是全场每个人各来一条。

     原来是「每 2 秒 × 每个在场的人各一条」——N 个人在线时服务器每秒要收
     N×(N-1)/2 条，而每条还会触发一次全量候选计算。50 人就 1200 条/秒，
     100 人约 5000 条/秒，免费额度的实例必挂。

     采样必须是**稳定**的：presence.py 要求同一个 ephemeral 连续出现 3 次
     才算数（PRESENCE_MIN_SIGHTINGS），每轮随机换一批就永远攒不够，
     谁也匹配不上。所以固定一批扫若干轮，再整体轮换。 */
  const SCAN_BATCH = 6;          // 每轮上报几个
  const ROUNDS_PER_BATCH = 5;    // 同一批扫几轮再换（要 > 3 才攒得够持续性）

  function pickBatch() {
    const pool = RS.ephemerals.filter(e => e.user_id !== USER);
    // 洗牌后取前 N；人少时就是全部
    for (let i = pool.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [pool[i], pool[j]] = [pool[j], pool[i]];
    }
    RS.scanBatch = pool.slice(0, SCAN_BATCH);
    RS.scanRound = 0;
  }

  function tick() {
    if (!RS.scanBatch || RS.scanRound >= ROUNDS_PER_BATCH) pickBatch();
    RS.scanRound++;
    RS.scanBatch.forEach(e => send({
      action: 'sighting',
      ephemeral_id: e.ephemeral_id,
      rssi: -58 - Math.floor(Math.random() * 8),
    }));
  }

  /* 外部用户版：手机上没有 Web Bluetooth，戒指连不了；手环也没接。
     把这两张卡藏掉，好过摆在那里点了没反应。 */
  if (!IS_JUDGE) {
    const hide = () => {
      document.getElementById('ring-connect-card')?.style.setProperty('display', 'none');
      document.querySelector('.card-health')?.style.setProperty('display', 'none');
      const rs = document.querySelector('.ring-status');
      if (rs) rs.style.display = 'none';
    };
    hide();
    setTimeout(hide, 600);   // ring.js 是在这之后挂卡片的，补一次
  }

  // 状态页在 iframe 里，拿不到主页面的会话——把后端地址与身份用查询串传进去，
  // 它据此拉 /api/devices 驱动粒子生命体；拉不到就自己随机，不会停在默认值上。
  (function wireStatusFrame() {
    const f = document.getElementById('status-page-frame');
    if (!f) return;
    const p = new URLSearchParams({ api: HTTP, user: USER });
    if (TOKEN) p.set('token', TOKEN);
    f.src = 'status_v11_mobile.html?' + p.toString();
  })();

  // 顶栏连接状态点一下 = 退出登录（真实用户才给，演示用户没什么可退的）
  if (session) {
    const rs = document.querySelector('.ring-status');
    if (rs) {
      rs.style.cursor = 'pointer';
      rs.title = '点击退出登录';
      rs.addEventListener('click', () => {
        if (confirm(`退出登录？\n当前账号：${session.email || USER}`)) logout();
      });
    }
  }

  // 问后端演示后门开着没有；拿不到就按"开着"处理（保持原行为）
  api('/api/auth/config')
    .then(c => {
      RS.demoAutoplay = c && c.demo_autoplay !== false;
      if (!RS.demoAutoplay) console.info('[RS] 演示自动播放已关闭，匹配与确认走真实链路');
    })
    .catch(() => { RS.demoAutoplay = true; });

  // ---------------------------------------------------------------- 启动
  connect();
  loadProfile();
  refreshDevices();
  setInterval(refreshDevices, 10000);
})();
