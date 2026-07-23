/* HardwareAdapter：统一硬件事件接口。
   Mock 与真实 Zilo SDK 实现同一接口，上层零改动（附录A规则3/4）。 */

export class HardwareAdapter {
  constructor() {
    this._buttonHandlers = [];
    this._imuHandlers = [];
    this._voiceHandlers = [];
    this._statusHandlers = [];
  }
  onButtonPress(cb) { this._buttonHandlers.push(cb); }
  onIMUData(cb) { this._imuHandlers.push(cb); }        // P1 占位
  onVoiceCapture(cb) { this._voiceHandlers.push(cb); } // P1 占位
  onStatusChange(cb) { this._statusHandlers.push(cb); }

  _emitButton(evt) { this._buttonHandlers.forEach(cb => cb(evt)); }
  _emitStatus(s) { this._statusHandlers.forEach(cb => cb(s)); }

  async connect() { throw new Error('not implemented'); }
  async disconnect() {}
}

/* MockAdapter：页面按钮触发，检测 400ms 内两次点击 = 双击确认，
   与真实戒指的 0x0703 按钮双击语义一致。 */
export class MockAdapter extends HardwareAdapter {
  constructor() {
    super();
    this._lastPress = 0;
    this.DOUBLE_WINDOW_MS = 400;
  }
  async connect() { this._emitStatus('mock_connected'); }

  /* UI 每次点击调用；返回 'first' | 'confirmed' 供 UI 做动效 */
  registerPress() {
    const t = Date.now();
    if (t - this._lastPress <= this.DOUBLE_WINDOW_MS) {
      this._lastPress = 0;
      this._emitButton({ type: 'double_press_confirm', source: 'mock' });
      return 'confirmed';
    }
    this._lastPress = t;
    return 'first';
  }
}
