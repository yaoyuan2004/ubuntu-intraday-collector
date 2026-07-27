#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/intraday-collector.service"
PYTHON_BIN="$(command -v python3 || true)"

if [[ -z "$PYTHON_BIN" ]]; then
  echo "[ERROR] 未找到 python3。请先执行：sudo apt update && sudo apt install -y python3" >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/data/backups" "$PROJECT_DIR/data/exports" "$SERVICE_DIR"
chmod 700 "$PROJECT_DIR/data" || true

"$PYTHON_BIN" "$PROJECT_DIR/collector.py" self-test
"$PYTHON_BIN" "$PROJECT_DIR/collector.py" validate

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Lightweight China market intraday collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON_BIN $PROJECT_DIR/collector.py run
Restart=on-failure
RestartSec=30
Environment=PYTHONUNBUFFERED=1
Nice=10
MemoryMax=128M
CPUQuota=25%%

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now intraday-collector.service

echo
echo "安装完成。"
echo "唯一日常维护文件：$PROJECT_DIR/config/instruments.csv"
echo "查看状态：systemctl --user status intraday-collector"
echo "查看日志：journalctl --user -u intraday-collector -f"
echo "检查数据：python3 $PROJECT_DIR/collector.py status"
echo
if command -v loginctl >/dev/null 2>&1; then
  if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    echo "为保证退出SSH后仍运行，请再执行："
    echo "  sudo loginctl enable-linger $USER"
  fi
fi
