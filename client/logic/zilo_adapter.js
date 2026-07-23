/* ZiloWebBluetoothAdapter：Chrome Web Bluetooth 直连真实戒指（ring_sound）。
   工作方式：notify 收到原始帧 -> hex 化 -> 转发后端 /ws/device/{user}；
             后端下发 {send_frame} -> 写入 write characteristic。
   按钮双击(0x0703)的业务判定在后端完成，本模块只做透传 + 本地回显。 */

import { HardwareAdapter } from './hardware_adapter.js';

/* TODO: 用 nRF Connect 连接戒指后，把实际的 Service/Characteristic UUID 填在这里。
   常见透传服务候选：0xFFE0(char 0xFFE1) / 0xFFF0 / Nordic UART 6e400001-... */
const SERVICE_UUID = '0000ffe0-0000-1000-8000-00805f9b34fb';
const WRITE_CHAR_UUID = '0000ffe1-0000-1000-8000-00805f9b34fb';
const NOTIFY_CHAR_UUID = '0000ffe1-0000-1000-8000-00805f9b34fb';

const CMD_BUTTON_DOUBLE_PRESS = 0x0703;
const MAX_RECONNECT = 3;

export class ZiloWebBluetoothAdapter extends HardwareAdapter {
  constructor(userId, wsBase) {
    super();
    this.userId = userId;
    this.wsBase = wsBase;          // 例如 ws://localhost:8000
    this.device = null;
    this.writeChar = null;
    this.ws = null;
    this._reconnects = 0;
  }

  async connect() {
    this._emitStatus('connecting');
    this.device = await navigator.bluetooth.requestDevice({
      acceptAllDevices: true,      // 现场可改为 filters: [{namePrefix: 'ring'}]
      optionalServices: [SERVICE_UUID],
    });
    this.device.addEventListener('gattserverdisconnected', () => this._onDisconnect());

    const server = await this.device.gatt.connect();
    const service = await server.getPrimaryService(SERVICE_UUID);
    this.writeChar = await service.getCharacteristic(WRITE_CHAR_UUID);
    const notifyChar = await service.getCharacteristic(NOTIFY_CHAR_UUID);
    await notifyChar.startNotifications();
    notifyChar.addEventListener('characteristicvaluechanged',
      e => this._onFrame(new Uint8Array(e.target.value.buffer)));

    this._openDeviceWs();
    this._emitStatus('connected');
  }

  _openDeviceWs() {
    this.ws = new WebSocket(`${this.wsBase}/ws/device/${this.userId}`);
    this.ws.onmessage = async (e) => {
      const msg = JSON.parse(e.data);
      if (msg.send_frame && this.writeChar) {
        await this.writeChar.writeValue(hexToBytes(msg.send_frame));
      }
    };
  }

  _onFrame(bytes) {
    const hex = bytesToHex(bytes);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ frame: hex }));
    }
    // 本地回显：让 UI 立即有反馈（业务判定仍以后端为准）
    if (bytes.length >= 2) {
      const cmd = (bytes[0] << 8) | bytes[1];
      if (cmd === CMD_BUTTON_DOUBLE_PRESS) {
        this._emitButton({ type: 'double_press_confirm', source: 'zilo' });
      }
    }
  }

  async _onDisconnect() {
    if (this._reconnects < MAX_RECONNECT) {
      this._reconnects += 1;
      this._emitStatus(`reconnecting_${this._reconnects}`);
      try {
        await this.device.gatt.connect();
        this._emitStatus('connected');
        return;
      } catch (_) { /* fallthrough */ }
      return this._onDisconnect();
    }
    this._emitStatus('offline');   // UI 提示切换 App 双确认兜底
  }

  async disconnect() {
    if (this.device?.gatt?.connected) this.device.gatt.disconnect();
    this.ws?.close();
  }
}

function bytesToHex(bytes) {
  return [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
}
function hexToBytes(hex) {
  const clean = hex.replace(/[\s-]/g, '');
  const out = new Uint8Array(clean.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(clean.substr(i * 2, 2), 16);
  return out;
}
