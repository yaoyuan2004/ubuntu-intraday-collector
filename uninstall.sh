#!/usr/bin/env bash
set -euo pipefail
SERVICE_FILE="$HOME/.config/systemd/user/intraday-collector.service"
systemctl --user disable --now intraday-collector.service 2>/dev/null || true
rm -f "$SERVICE_FILE"
systemctl --user daemon-reload
echo "服务已卸载。项目目录和 data/intraday.db 未删除。"
