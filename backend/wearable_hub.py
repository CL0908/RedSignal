"""统一可穿戴设备数据中心 —— 合并 Zilo Ring + Xiaomi Watch（Gadgetbridge）数据。

职责：
  1. 维护每个用户的设备状态（哪些设备在线、最新数据）
  2. 提供统一快照供前端展示
  3. 接收来自不同数据源的更新

数据流：
  Zilo Ring → WebSocket /ws/device/{user_id} → zilo_protocol → wearable_hub
  Xiaomi Watch → Gadgetbridge DB export → POST /api/health/sync → wearable_hub
                 或 Android 转发 → WebSocket → wearable_hub
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .gadgetbridge import WatchHealthSnapshot

log = logging.getLogger("wearable_hub")


@dataclass
class RingStatus:
    """Zilo Ring 实时状态。"""
    connected: bool = False
    firmware: str = ""
    battery_percent: int = -1
    charging: bool = False
    model: str = "ring_sound"
    last_button_press: Optional[datetime] = None
    last_gesture: Optional[str] = None
    last_gesture_at: Optional[datetime] = None
    imu_active: bool = False
    # 最近 IMU 摘要
    last_accel: Optional[tuple[float, float, float]] = None
    last_gyro: Optional[tuple[float, float, float]] = None


@dataclass
class WatchStatus:
    """小米手表（via Gadgetbridge）实时状态。"""
    connected: bool = False
    device_name: str = "Xiaomi Watch"
    heart_rate_bpm: int = 0
    heart_rate_at: Optional[datetime] = None
    today_steps: int = 0
    spo2_percent: int = 0
    stress_level: int = 0
    sleep_hours: float = 0.0
    battery_percent: int = -1


@dataclass
class UnifiedDeviceSnapshot:
    """用户所有可穿戴设备的统一快照——前端直接消费。"""
    user_id: str
    ring: RingStatus = field(default_factory=RingStatus)
    watch: WatchStatus = field(default_factory=WatchStatus)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        """JSON 序列化供 WebSocket/REST 返回。"""
        return {
            "user_id": self.user_id,
            "updated_at": self.updated_at.isoformat(),
            "ring": {
                "connected": self.ring.connected,
                "firmware": self.ring.firmware,
                "battery_percent": self.ring.battery_percent,
                "charging": self.ring.charging,
                "model": self.ring.model,
                "last_button_press": (self.ring.last_button_press.isoformat()
                                      if self.ring.last_button_press else None),
                "last_gesture": self.ring.last_gesture,
                "last_gesture_at": (self.ring.last_gesture_at.isoformat()
                                    if self.ring.last_gesture_at else None),
                "imu_active": self.ring.imu_active,
                "last_accel": self.ring.last_accel,
                "last_gyro": self.ring.last_gyro,
            },
            "watch": {
                "connected": self.watch.connected,
                "device_name": self.watch.device_name,
                "heart_rate_bpm": self.watch.heart_rate_bpm,
                "heart_rate_at": (self.watch.heart_rate_at.isoformat()
                                  if self.watch.heart_rate_at else None),
                "today_steps": self.watch.today_steps,
                "spo2_percent": self.watch.spo2_percent,
                "stress_level": self.watch.stress_level,
                "sleep_hours": round(self.watch.sleep_hours, 1),
                "battery_percent": self.watch.battery_percent,
            },
        }


class WearableHub:
    """全局单例：管理所有用户的设备数据。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, UnifiedDeviceSnapshot] = {}

    def get(self, user_id: str) -> UnifiedDeviceSnapshot:
        if user_id not in self._snapshots:
            self._snapshots[user_id] = UnifiedDeviceSnapshot(user_id=user_id)
        return self._snapshots[user_id]

    # ---- Ring 更新 ----

    def ring_connected(self, user_id: str, firmware: str = "",
                       battery: int = -1, model: str = "ring_sound") -> None:
        snap = self.get(user_id)
        snap.ring.connected = True
        snap.ring.firmware = firmware
        snap.ring.battery_percent = battery
        snap.ring.model = model
        snap.updated_at = datetime.now(timezone.utc)

    def ring_disconnected(self, user_id: str) -> None:
        snap = self.get(user_id)
        snap.ring.connected = False
        snap.ring.imu_active = False
        snap.updated_at = datetime.now(timezone.utc)

    def ring_button_press(self, user_id: str) -> None:
        snap = self.get(user_id)
        snap.ring.last_button_press = datetime.now(timezone.utc)
        snap.updated_at = datetime.now(timezone.utc)

    def ring_gesture(self, user_id: str, gesture: str) -> None:
        snap = self.get(user_id)
        snap.ring.last_gesture = gesture
        snap.ring.last_gesture_at = datetime.now(timezone.utc)
        snap.updated_at = datetime.now(timezone.utc)

    def ring_imu(self, user_id: str,
                 accel: tuple[float, float, float],
                 gyro: tuple[float, float, float]) -> None:
        snap = self.get(user_id)
        snap.ring.imu_active = True
        snap.ring.last_accel = accel
        snap.ring.last_gyro = gyro
        snap.updated_at = datetime.now(timezone.utc)

    # ---- Watch 更新 ----

    def watch_sync(self, user_id: str, health: WatchHealthSnapshot) -> None:
        """从 Gadgetbridge DB 同步后更新。"""
        snap = self.get(user_id)
        snap.watch.connected = True
        snap.watch.device_name = health.device_name
        snap.watch.today_steps = health.today_steps
        snap.watch.sleep_hours = health.sleep_hours
        if health.last_heart_rate:
            snap.watch.heart_rate_bpm = health.last_heart_rate.bpm
            snap.watch.heart_rate_at = health.last_heart_rate.timestamp
        if health.last_spo2:
            snap.watch.spo2_percent = health.last_spo2.spo2_percent
        if health.last_stress:
            snap.watch.stress_level = health.last_stress.stress_level
        snap.updated_at = datetime.now(timezone.utc)

    def watch_realtime_hr(self, user_id: str, bpm: int) -> None:
        """Android 转发的实时心率。"""
        snap = self.get(user_id)
        snap.watch.connected = True
        snap.watch.heart_rate_bpm = bpm
        snap.watch.heart_rate_at = datetime.now(timezone.utc)
        snap.updated_at = datetime.now(timezone.utc)

    def watch_realtime_steps(self, user_id: str, steps: int) -> None:
        snap = self.get(user_id)
        snap.watch.connected = True
        snap.watch.today_steps = steps
        snap.updated_at = datetime.now(timezone.utc)

    def watch_disconnected(self, user_id: str) -> None:
        snap = self.get(user_id)
        snap.watch.connected = False
        snap.updated_at = datetime.now(timezone.utc)


# 全局单例
wearable_hub = WearableHub()
