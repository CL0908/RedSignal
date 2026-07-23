# Zilo 戒指（"ring" / model=ring_sound）逆向 —— 完整协议 + 真机验证

**状态：✅ 完全攻破并真机验证通过（2026-07-23）。**

来源：① macOS + bleak 直连抓包；② 官方 `demo.apk`（uni-app/DCloud）内
`assets/apps/__UNI__308E163/www/app-service.js` 的帧编解码器逆向。
真实实现已落地 `backend/zilo_protocol.py`（43 条测试全绿）。

---

## 1. BLE 传输层

- 广播名：`ring`；型号：`ring_sound`
- **Nordic UART Service (NUS)** 透传

| 角色 | UUID |
|---|---|
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| Notify（上行 戒指→手机） | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |
| Write（下行 手机→戒指） | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |

第二个 service `6f5b8a2e-3d1c-4f6a-9b2e-7d6c5a4b3c2d`：疑似 OTA/配置，read 返回 Invalid Handle。

## 2. 帧格式（全大端）★核心

```
off 0    uint8   magic = 0x3F
off 1-2  uint16  version = 0x0004（常量）
off 3-4  uint16  command
off 5-8  uint32  payload length
off 9-10 uint16  CRC16(payload)      ← crc16()，CCITT 变体
off 11+  payload
```

- CRC16 = APK `ue()` 逐字节实现（种子 0xFFFF）。空 payload 的 CRC = 0xFFFF。
- 例：获取系统信息 `0x0101` 完整帧 = `3f0004010100000000ffff`
- **旧 `zilo_protocol.py` 假设「2 字节大端 cmd」是错的**，所以真机之前完全不响应。

## 3. 完整命令表（摘自 APK）

| cmd | 含义 | 方向/payload |
|---|---|---|
| 0x0101 / 0x0102 | 获取系统信息 / 系统信息 | 0x0102: err+固件+systemTime+存储+**电量%**+充电+SN+CPU+型号 |
| 0x0103 / 0x0104 | 修改系统配置 / 结果 | |
| 0x0301/0x0303 | 戒指日志（存储信息/数据） | |
| **0x0401 / 0x0402** | **校时请求（戒指发）/ 校时应答（手机回）** | 戒指开机反复发 0x0401 求时间；不回就一直发（早先看到的"心跳"即此） |
| 0x0403 / 0x0404 | 毫秒级校时 | |
| 0x0501/0503/0505/0509 | 录音列表/开始提取/数据帧/快速播放 | 戒指可**录音**并回传 |
| **0x0601 / 0x0603** | **开启 / 停止** 实时六轴上报 | |
| **0x0605** | **sensor report frame（实时六轴）** | err+seqStart(u32)+frameCount(u16)+frameSize(u16)+帧[ts(u32),accel×3 int16,gyro×3 int16] |
| 0x0606–0x060F | sensor 文件传输/清除 | |
| 0x0701 | gesture recognition result | payload[0-3]=ts |
| 0x0702 | sensor gesture result | payload[4]=gestureId：1=后旋 2=前旋 3=挥手 |
| **0x0703** | **key double press result（按钮双击）** | P0 确认信号；payload=u32 事件序号 |
| 0x1005/0x1105/0x20xx | OTA | |

错误码：0成功 1未知 2忙 3文件不存在 4命令组不存在 5命令不存在 6超时 7参数异常 8通讯异常 9Flash不足。

## 4. 真机验证结果（2026-07-23，tools/ring_session.py）

- 发 `0x0101` → 收 `0x0102`：**固件 `V2.000.0001.0015`，电量 `96%`，未充电，型号 `ring_sound`**。
- 主动回 `0x0402` 校时后，戒指停止刷 `0x0401`。✅
- 双击按钮 → 收 **`0x0703` 共 27+ 次，全部解析正确**，payload 为自增事件序号
  （00003d43, 3d44, 3d45 … 单调递增）。✅ P0 确认信号端到端打通。
- 手势 `0x0702` 亦收到（idle 等）。✅

## 5. 对 RedSignal 的意义

- `backend/zilo_protocol.py` 已按真实协议重写：`build_frame/parse_frame/crc16/
  frame_to_event/parse_sys_info/build_time_sync_ack`。
- `main.py` 设备网关 `/ws/device/{user_id}` 收到浏览器转发的真帧后，`0x0703` 走**与 Mock
  完全相同的 confirm 入口** —— 现在能用真戒指按钮做双向确认。
- ⚠️ 客户端连上后**应主动回 0x0402 校时**，否则戒指持续刷 0x0401（`main.py` 可加）。
- 戒指还能取**电量/固件/六轴/手势/录音** —— 可喂给你们 App 的统一展示。

## 6. 工具清单（tools/）

| 脚本 | 作用 |
|---|---|
| `ble_scan.py [秒]` | 扫描周边 BLE |
| `ble_watch.py [秒]` | 连续监视，过滤 Apple 噪声，戒指一广播就冒出 |
| `ble_dump.py <名/址>` | 导出完整 GATT 表 |
| `ble_probe.py …` | 发命令/扫命令空间/受动监听（早期探索用） |
| `ble_capture.py [秒]` | 常驻自动重连抓包，新种帧高亮 |
| **`ring_session.py [秒]`** | **用真实协议正确对话：回校时→取系统信息→开六轴→抓按钮/手势** |

复现：戒指贴 Mac、双击唤醒，`​.venv/bin/python -u tools/ring_session.py 60`。
