/* ClientSession：用户通道 WebSocket 封装。纯逻辑，无 DOM 依赖，可移植到移动端。 */

export class ClientSession {
  constructor(userId, wsBase) {
    this.userId = userId;
    this.wsBase = wsBase;
    this.ws = null;
    this.handlers = {};        // type -> [cb]
    this.state = 'BLUE_OFFLINE';
    this.mode = 'off';
  }

  on(type, cb) {
    (this.handlers[type] = this.handlers[type] || []).push(cb);
  }

  connect() {
    this.ws = new WebSocket(`${this.wsBase}/ws/user/${this.userId}`);
    this.ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'state') { this.state = msg.state; this.mode = msg.mode; }
      (this.handlers[msg.type] || []).forEach(cb => cb(msg));
    };
    this.ws.onclose = () => setTimeout(() => this.connect(), 1500);
  }

  _send(obj) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  setMode(mode) { this._send({ action: 'set_mode', mode }); }
  reportSighting(ephemeralId, rssi = -60) {
    this._send({ action: 'sighting', ephemeral_id: ephemeralId, rssi });
  }
  mockButtonConfirm() { this._send({ action: 'mock_button' }); }
  appConfirm() { this._send({ action: 'app_confirm' }); }
  clearData() { this._send({ action: 'clear_data' }); }
}
