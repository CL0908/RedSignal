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
  const USER = params.get('user') || 'u_demo_a';
  const HTTP = location.origin;
  const WS = HTTP.replace(/^http/, 'ws');

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
    applying: false,   // true = 正在把后端状态回灌 UI，此时不要再往回发 set_mode
    // 会话按昵称索引（index.html 的 conversations 就是这么存的），
    // 但后端路由用 encounter_id——前端始终不需要知道对方 user_id。
    encounterByName: {},
    analyzed: {},      // encounter_id -> true，避免同一段聊天反复送去学偏好
  };
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
    const r = await fetch(HTTP + path, opts);
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
      '双击戒指确认（App 代按）</button>' +
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
    btn.textContent = '双击戒指确认（App 代按）';
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
    RS.ws = new WebSocket(`${WS}/ws/user/${USER}`);

    RS.ws.onopen = () => {
      RS.online = true;
      setRingStatus('已连接后端 · ' + USER, true);
      // 页面上当前选中的模式即时同步给后端
      const sel = document.querySelector('.mode-card.selected')?.dataset.mode;
      if (sel) send({ action: 'set_mode', mode: MODE_TO_API[sel] });
    };

    RS.ws.onclose = () => {
      RS.online = false;
      setRingStatus('后端离线 · 演示模式', false);
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
        // reason: matched / no_candidate / cooldown / unknown_ephemeral ...
        if (m.reason && m.reason !== 'matched') {
          const s = document.querySelector('.radar-status strong');
          if (s) s.textContent = '正在匿名发现 · ' + m.reason;
        }
        break;
      case 'match_notice':
        RS.pairId = m.pair_id;
        RS.selfConfirmed = false;
        showBanner(`${m.text}（适配 ${m.match_score} · ${m.proximity_band}）`, true);
        if (typeof window.playSayHiOnce === 'function') window.playSayHiOnce();
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

  async function refreshDevices() {
    try {
      const d = await api(`/api/devices/${USER}`);
      applyWatch(d.watch || {});
      if (d.ring && d.ring.connected) {
        setRingStatus(`戒指已连接 · ${d.ring.battery_percent}%`, true);
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
        if (who) who.title = `${p.nickname} · ${USER}`;
      }
    } catch { /* 静默 */ }
  }

  const pushTags = () => {
    const t = getTags();
    if (t) jsonPatch(`/api/profile/${USER}`, { interest_tags: t.slice() });
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
      jsonPatch(`/api/profile/${USER}`, { wish: currentWish });
    }
  });

  wrap('startDiscovery', () => {
    // 开始发现 = 保证处于可见模式 + 周期性上报 BLE 扫描结果
    const mode = document.querySelector('.mode-card.selected')?.dataset.mode || 'red';
    if (mode === 'blue') window.selectMode('red');
    else send({ action: 'set_mode', mode: MODE_TO_API[mode] });
    startScan();
  });

  wrap('stopDiscovery', () => stopScan());

  // 匿名编号：用后端真实的 ephemeral_id 尾号，而不是纯随机
  wrap('updateAnonCode', () => {
    const mine = RS.ephemerals.find(e => e.user_id === USER);
    if (!mine) return;
    const el = document.getElementById('anon-code');
    if (el) el.textContent = '#' + mine.ephemeral_id.slice(-6).toUpperCase();
  });

  // 点吉祥物 = Mock 戒指双击（无硬件时的确认兜底）
  wrap('onMascotClick', () => {
    if (RS.pairId && !RS.selfConfirmed) send({ action: 'mock_button' });
  });

  // ------------------------------------------------------------ 聊天收发
  /* sendChatMessage 不能用 wrap()：原函数会先清空输入框，
     等到 after 钩子跑的时候文本已经没了。所以整体替换，先取文本再放行。 */
  const origSendChat = window.sendChatMessage;
  if (typeof origSendChat === 'function') {
    window.sendChatMessage = function (event) {
      const input = document.getElementById('chat-input');
      const text = input ? input.value.trim() : '';
      const name = activeChat();
      const r = origSendChat.apply(this, arguments);   // 本地乐观渲染
      const eid = RS.encounterByName[name];
      if (text && eid) send({ action: 'chat_send', encounter_id: eid, text });
      return r;
    };
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
    RS.scanTimer = setInterval(tick, 2000);
  }

  function stopScan() {
    if (RS.scanTimer) clearInterval(RS.scanTimer);
    RS.scanTimer = null;
  }

  function tick() {
    // presence.py 需要"持续性"证据：每轮把在场的匿名 ID 各上报一次
    RS.ephemerals
      .filter(e => e.user_id !== USER)
      .forEach(e => send({
        action: 'sighting',
        ephemeral_id: e.ephemeral_id,
        rssi: -58 - Math.floor(Math.random() * 8),
      }));
  }

  // ---------------------------------------------------------------- 启动
  connect();
  loadProfile();
  refreshDevices();
  setInterval(refreshDevices, 10000);
})();
