#!/usr/bin/env bash
# DpskLite-WebUI Docker 启动 / 重建脚本
# 用法：
#   bash /home/cilca/dpsklite-run.sh        # 删除旧容器并按参数重建（数据/模型卷保留）
set -e

KEY=$(cat /home/cilca/.dpsklite_secret)

# 删除旧容器（数据在绑定卷里，不丢失）
docker rm -f dpsklite-webui 2>/dev/null || true

docker run -d --name dpsklite-webui -p 5000:5000 -u 1003:1003 \
  -e DPSKLITE_WEBUI_SECRET_KEY="$KEY" \
  -v /home/cilca/DpskLite-WebUI/data:/app/data \
  -v /home/cilca/DpskLite-WebUI/models:/app/models \
  dpsklite-webui:latest

echo "OK: dpsklite-webui container started"
