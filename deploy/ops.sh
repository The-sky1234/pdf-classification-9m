#!/usr/bin/env bash
# 运维入口：./ops.sh {start|stop|restart|health|logs|ps}；需在deploy/下执行且已配置.env
set -euo pipefail
cd "$(dirname "$0")"
cmd="${1:-health}"
case "$cmd" in
  start) docker compose up -d --build ;;
  stop) docker compose stop ;;
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
  *) echo "usage: ops.sh {start|stop|restart|health|logs|ps}"; exit 2 ;;
esac
