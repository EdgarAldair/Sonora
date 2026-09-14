#!/bin/sh
set -e
cd "$(dirname "$0")"
docker tag sonora-server "sonora-server:backup-$(date +%Y-%m-%d)" 2>/dev/null || true
docker compose pull bgutil-provider
docker compose build --no-cache sonora
docker compose up -d
docker exec sonora-server pip list 2>/dev/null | grep -i -E "yt-dlp|bgutil"
curl -s "http://$(docker compose port sonora 8000)/health"
echo
