#!/usr/bin/env bash
set -euo pipefail

# 现场演示预设：本地免费转写，不上传戒指录音。
export REDSIGNAL_DEMO_MODE="${REDSIGNAL_DEMO_MODE:-1}"
# 不设的话默认是「开」，会自动配对并替双方完成确认——那会掩盖真实链路的
# 故障（戒指没连通也显示成功），而且与线上当前设置相反。这里显式关掉，
# 要演自动流程时用 REDSIGNAL_DEMO_AUTOPLAY=1 覆盖。
export REDSIGNAL_DEMO_AUTOPLAY="${REDSIGNAL_DEMO_AUTOPLAY:-0}"
export RING_TRANSCRIBER="${RING_TRANSCRIBER:-local}"
export RING_WHISPER_MODEL="${RING_WHISPER_MODEL:-tiny}"

echo "Demo 用户 A: http://localhost:8000/app/?user=u_demo_a"
echo "Demo 用户 B: http://localhost:8000/app/?user=u_demo_b"
echo "请用两个浏览器窗口/隐身窗口分别打开上面两个地址。"

exec .venv312/bin/uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
