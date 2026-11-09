#!/usr/bin/env bash
# 运维入口，在 deploy/ 目录下用，读同目录 .env。
#
#   ./ops.sh init                    建目录、改属主（首次）
#   ./ops.sh check                   查驱动、显卡、容器 GPU、目录、磁盘
#   ./ops.sh build [api|ocr|infer]   构建镜像，不给参数就全部
#   ./ops.sh fetch-models            下载 OCR 和分类模型到 MODEL_DIR（只需联网一次）
#   ./ops.sh up                      起 postgres、api、cpu-worker（波次 0 + 归档）
#   ./ops.sh load 清单 [load 参数]    主件清单入库，例如 --map 'D:\共享=/mnt/nas/共享'
#   ./ops.sh wave ocr|classify|off   切换 GPU 波次（同一时间只开一个）
#   ./ops.sh autopilot               按队列自动切 GPU 波次，全部做完退出
#   ./ops.sh status                  各阶段数量
#   ./ops.sh export                  全部结论导出到 DATA_DIR/exports
#   ./ops.sh requeue [阶段]           failed 的重新排队
#   ./ops.sh backup                  pg_dump 到 DATA_DIR/backups
#   ./ops.sh logs [服务]  /  ps  /  stop
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "缺少 deploy/.env：cp .env.example .env 后按注释填写"; exit 2; }
set -a
# shellcheck disable=SC1091
. ./.env
set +a

dc() { docker compose "$@"; }
gpu_dc() { docker compose --profile ocr --profile classify "$@"; }
phase1() { dc exec -T api python -m phase1 "$@"; }

pending() {
  phase1 status | python3 -c "import json,sys; print(json.load(sys.stdin)['by_stage'].get('$1', 0))"
}

wave() {
  case "$1" in
    ocr) gpu_dc stop classify-worker >/dev/null 2>&1 || true; gpu_dc up -d --no-build ocr-worker ;;
    classify) gpu_dc stop ocr-worker >/dev/null 2>&1 || true; gpu_dc up -d --no-build classify-worker ;;
    off) gpu_dc stop ocr-worker classify-worker >/dev/null 2>&1 || true ;;
    *) echo "wave ocr|classify|off"; exit 2 ;;
  esac
}

cmd="${1:-help}"
shift || true
case "$cmd" in
  init)
    mkdir -p "$DATA_DIR"/{locks,exports,config,backups} "$MODEL_DIR"/{paddleocr,hf} "$PG_DATA_DIR" "$ARCHIVE_ROOT"
    chown "${APP_UID:-10001}:${APP_GID:-10001}" "$DATA_DIR"/{locks,exports} "$MODEL_DIR"/{paddleocr,hf} "$ARCHIVE_ROOT"
    chown "${SUDO_USER:-$(id -un)}" "$DATA_DIR/backups" .env
    chmod 600 .env
    echo "目录已建好。源目录 $SOURCE_ROOT 只读挂载，需要 uid ${APP_UID:-10001} 可读"
    ;;
  check)
    bash ./gpu_check.sh
    ;;
  build)
    case "${1:-all}" in
      api) dc build api ;;
      ocr) gpu_dc build ocr-worker ;;
      infer) gpu_dc build classify-worker ;;
      all) dc build api && gpu_dc build ocr-worker classify-worker ;;
      *) echo "build api|ocr|infer"; exit 2 ;;
    esac
    ;;
  fetch-models)
    # 只下载，不上卡：不走 compose 的 GPU 预留，驱动和 nvidia-container-toolkit 没配好也能先下
    user="${APP_UID:-10001}:${APP_GID:-10001}"
    docker run --rm --user "$user" -e OCR_USE_GPU=0 -v "$MODEL_DIR/paddleocr:/home/app/.paddleocr" \
      "pdfcls-ocr:${IMAGE_TAG:-latest}" python deploy/fetch_models.py ocr
    docker run --rm --user "$user" -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 \
      -e HF_ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}" \
      -e EMBED_MODEL="${EMBED_MODEL:-BAAI/bge-m3}" -e RERANK_MODEL="${RERANK_MODEL:-BAAI/bge-reranker-v2-m3}" \
      -v "$MODEL_DIR/hf:/models/hf" "pdfcls-infer:${IMAGE_TAG:-latest}" python deploy/fetch_models.py infer
    ;;
  up)
    dc up -d --no-build postgres api cpu-worker
    dc ps
    ;;
  load)
    [ $# -ge 1 ] || { echo "ops.sh load 清单文件 [--map 旧=新 ...]"; exit 2; }
    list="$(realpath "$1")"; shift
    dc run --rm --no-deps -v "$list:/data/input/$(basename "$list"):ro" cpu-worker \
      python -m phase1 load --masters "/data/input/$(basename "$list")" "$@"
    ;;
  wave)
    wave "${1:-}"
    ;;
  autopilot)
    # 当前波次的队列清空才切换，避免两个模型来回加载。cpu 还在出活时继续等。
    current=""
    while true; do
      ocr=$(pending ocr); cls=$(pending classify); cpu=$(pending cpu)
      if [ "$current" = ocr ] && [ "$ocr" -gt 0 ]; then :
      elif [ "$current" = classify ] && [ "$cls" -gt 0 ]; then :
      elif [ "$ocr" -gt 0 ]; then current=ocr; wave ocr
      elif [ "$cls" -gt 0 ]; then current=classify; wave classify
      elif [ "$cpu" -eq 0 ]; then wave off; echo "$(date '+%F %T') GPU 波次全部完成"; break
      fi
      echo "$(date '+%F %T') gpu=${current:-none} cpu=$cpu ocr=$ocr classify=$cls"
      sleep "${AUTOPILOT_INTERVAL:-120}"
    done
    ;;
  status)
    phase1 status
    ;;
  export)
    out="/data/exports/decisions-$(date +%Y%m%d-%H%M%S).jsonl"
    phase1 export --out "$out"
    echo "宿主机路径：$DATA_DIR/exports/$(basename "$out")"
    ;;
  requeue)
    if [ $# -ge 1 ]; then phase1 requeue --stage "$1"; else phase1 requeue; fi
    ;;
  backup)
    f="$DATA_DIR/backups/pdfcls-$(date +%Y%m%d-%H%M%S).dump"
    dc exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$f.part" && mv "$f.part" "$f"
    ls -lh "$f"
    ;;
  logs) gpu_dc logs -f --tail=200 "$@" ;;
  ps) gpu_dc ps -a ;;
  stop) wave off; dc stop ;;
  *) sed -n '2,19p' "$0"; [ "$cmd" = help ] || exit 2 ;;
esac
