#!/usr/bin/env bash
# 运维入口。start 只起 CPU 栈。GPU 用 wave-ocr / wave-classify / wave-mineru，一次只开一个波次。
# ./ops.sh {start|stop|restart|health|logs|ps|wave-ocr|wave-classify|wave-mineru}
set -euo pipefail
cd "$(dirname "$0")"
cmd="${1:-health}"
stop_gpu() {
  docker compose --profile ocr-wave stop ocr-worker || true
  docker compose --profile classify-wave stop classify-worker || true
  docker compose --profile mineru-wave stop mineru-worker || true
}
case "$cmd" in
  start) docker compose up -d --build ;;
  wave-ocr) stop_gpu; docker compose --profile ocr-wave up -d --build ocr-worker ;;
  wave-classify) stop_gpu; docker compose --profile classify-wave up -d --build classify-worker ;;
  wave-mineru) stop_gpu; docker compose --profile mineru-wave up -d --build mineru-worker ;;
  stop) stop_gpu; docker compose stop ;;
  restart) docker compose restart "${2:-}" ;;
  ps) docker compose ps ;;
  logs) docker compose logs -f --tail=200 "${2:-}" ;;
  health)
    set +e
    curl -sf -H "X-API-Key: ${API_KEY:?need source .env}" "http://127.0.0.1:${API_PORT:-8000}/taxonomy" && echo && echo "API OK"
    docker compose exec -T postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"
    docker compose exec -T redis redis-cli ping
    docker compose ps --format json | python3 -c "import json,sys; [print(r['Name'], r['State'], r.get('Health','')) for r in map(json.loads, sys.stdin)]"
    ;;
  *) echo "usage: ops.sh {start|stop|restart|health|logs|ps|wave-ocr|wave-classify|wave-mineru}"; exit 2 ;;
esac
