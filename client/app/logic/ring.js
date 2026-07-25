/* Zilo 戒指 Web Bluetooth 直连。
 *
 * 工作方式：notify 收到原始帧 → hex → 转发后端 /ws/device/{user}；
 *          后端下发 {send_frame} → 写入 write characteristic。
 * 按钮双击(0x0703) 的业务判定全在后端，本模块只做透传——
 * 这样 Mock 按钮和真戒指走的是同一个 confirm 入口（附录A 规则6）。
 *
 * UUID 来自 tools/RING_FINDINGS.md 的真机验证（2026-07-23），不是猜的：
 * 戒指用 Nordic UART Service 透传，广播名 "ring"，型号 ring_sound。
 *
 * ⚠️ iOS Safari 不支持 Web Bluetooth（任何版本都不支持）。
 *    iPhone 上要连真戒指，得用 Bluefy / WebBLE 这类自带蓝牙栈的浏览器。
 *    桌面 Chrome/Edge 与 Android Chrome 正常。
 */
(function () {
  'use strict';

  const SERVICE_UUID = '6e400001-b5a3-f393-e0a9-e50e24dcca9e';
  const WRITE_CHAR   = '6e400002-b5a3-f393-e0a9-e50e24dcca9e';  // 手机 → 戒指
  const NOTIFY_CHAR  = '6e400003-b5a3-f393-e0a9-e50e24dcca9e';  // 戒指 → 手机

  const Ring = {
    supported: typeof navigator !== 'undefined' && !!navigator.bluetooth,
    device: null,
    writeChar: null,
    ws: null,
    connected: false,
    battery: null,
    lastFrame: null,
  };
  window.Ring = Ring;

  function bytesToHex(buf) {
    return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
  }
  function hexToBytes(hex) {
    const out = new Uint8Array(hex.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
    return out;
  }

  function status(text, kind) {
    const el = document.getElementById('ring-connect-status');
    if (el) {
      el.textContent = text;
      el.dataset.kind = kind || '';
    }
    const top = document.querySelector('.ring-status');
    if (top && kind === 'ok') {
      const n = top.childNodes[top.childNodes.length - 1];
      if (n) n.nodeValue = Ring.battery != null ? `戒指已连接 · ${Ring.battery}%` : '戒指已连接';
      const dot = top.querySelector('.ring-dot');
      if (dot) dot.style.background = '';
    }
    if (typeof window.toast === 'function' && kind === 'err') window.toast(text);
  }

  /* ---- 设备通道：把原始帧转发给后端，后端回什么就写什么给戒指 ---- */
  function openDeviceChannel() {
    const RS = window.RS;
    if (!RS) return null;
    const base = (document.querySelector('meta[name="rs-api-base"]')?.content || '').trim()
                 || location.origin;
    const wsBase = base.replace(/^http/, 'ws');
    const q = RS.token ? `?token=${encodeURIComponent(RS.token)}` : '';
    const ws = new WebSocket(`${wsBase}/ws/device/${encodeURIComponent(RS.userId)}${q}`);
    ws.onmessage = async (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      if (m.send_frame && Ring.writeChar) {
        try {
          await Ring.writeChar.writeValue(hexToBytes(m.send_frame));
        } catch (err) {
          console.warn('[Ring] 写入失败', err);
        }
      }
    };
    ws.onclose = () => { if (Ring.connected) status('设备通道断开', 'err'); };
    return ws;
  }

  async function connect() {
    if (!Ring.supported) {
      status('这个浏览器不支持 Web Bluetooth。iPhone 请用 Bluefy 或 WebBLE 打开；' +
             '电脑用 Chrome/Edge，安卓用 Chrome。', 'err');
      return;
    }
    try {
      status('正在搜索戒指…', 'busy');
      Ring.device = await navigator.bluetooth.requestDevice({
        // 戒指广播名是 "ring"；同时按服务过滤，避免列出一堆无关设备
        filters: [{ namePrefix: 'ring' }, { services: [SERVICE_UUID] }],
        optionalServices: [SERVICE_UUID],
      });
      Ring.device.addEventListener('gattserverdisconnected', onDisconnected);

      status('连接中…', 'busy');
      const server = await Ring.device.gatt.connect();
      const service = await server.getPrimaryService(SERVICE_UUID);
      Ring.writeChar = await service.getCharacteristic(WRITE_CHAR);
      const notifyChar = await service.getCharacteristic(NOTIFY_CHAR);

      await notifyChar.startNotifications();
      notifyChar.addEventListener('characteristicvaluechanged', (ev) => {
        const hex = bytesToHex(ev.target.value.buffer);
        Ring.lastFrame = hex;
        if (Ring.ws && Ring.ws.readyState === WebSocket.OPEN) {
          Ring.ws.send(JSON.stringify({ frame: hex }));
        }
      });

      Ring.ws = openDeviceChannel();
      Ring.connected = true;
      status(`已连接 ${Ring.device.name || 'ring'}`, 'ok');
      renderButton();
    } catch (e) {
      // 用户点了取消也会走到这里，不当成错误刷屏
      const msg = String(e && e.message || e);
      if (/cancel|User cancelled/i.test(msg)) { status('', ''); return; }
      status('连接失败：' + msg, 'err');
      console.warn('[Ring]', e);
    }
  }

  function onDisconnected() {
    Ring.connected = false;
    Ring.writeChar = null;
    try { Ring.ws && Ring.ws.close(); } catch { /* 已经关了 */ }
    Ring.ws = null;
    status('戒指已断开', 'err');
    renderButton();
  }

  function disconnect() {
    try { Ring.device?.gatt?.disconnect(); } catch { /* 本来就没连 */ }
    onDisconnected();
  }

  Ring.connect = connect;
  Ring.disconnect = disconnect;

  /* ---------------------------------------------------------- 首页按钮 */
  function renderButton() {
    const btn = document.getElementById('ring-connect-btn');
    if (!btn) return;
    btn.textContent = Ring.connected ? '断开戒指' : '连接戒指';
    btn.onclick = Ring.connected ? disconnect : connect;
  }

  function mount() {
    // 插在「今天想找」卡片前面：这是首页最上面的一张卡，位置最显眼
    const anchor = document.querySelector('.card-wish');
    if (!anchor || document.getElementById('ring-connect-card')) return;

    const card = document.createElement('div');
    card.className = 'card';
    card.id = 'ring-connect-card';
    card.innerHTML =
      '<div class="card-label"><span>戒指</span><span id="ring-connect-hint"></span></div>' +
      '<div style="display:flex;align-items:center;gap:10px;margin-top:2px">' +
      '  <button id="ring-connect-btn" type="button" style="flex:0 0 auto;' +
      'padding:9px 18px;border:1.2px solid rgba(44,38,32,.28);border-radius:14px;' +
      'background:var(--card);color:var(--text);font:inherit;font-size:13px;' +
      'font-weight:600">连接戒指</button>' +
      '  <div id="ring-connect-status" style="font-size:12px;color:var(--sub);' +
      'line-height:1.5;flex:1"></div>' +
      '</div>';
    anchor.parentNode.insertBefore(card, anchor);

    renderButton();
    if (!Ring.supported) {
      document.getElementById('ring-connect-hint').textContent = '此浏览器不支持';
      status('iPhone Safari 不支持 Web Bluetooth。用 Bluefy / WebBLE 打开，' +
             '或在电脑 Chrome、安卓 Chrome 上连。', '');
      const b = document.getElementById('ring-connect-btn');
      if (b) b.style.opacity = '.55';
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
