#!/usr/bin/env bash
# 上线前检查：驱动、显卡、容器能否用 GPU、目录、磁盘。有 [FAIL] 时退出码为 1。
# 在 deploy/ 下由 ops.sh check 调用（会先读 .env）；单独跑也行，目录检查会跳过没设置的变量。
set -u
pass=0; fail=0
ok() { echo "[OK]   $1"; pass=$((pass + 1)); }
bad() { echo "[FAIL] $1"; fail=$((fail + 1)); }
warn() { echo "[WARN] $1"; }

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv
  idx="${GPU_DEVICE:-0}"
  name=$(nvidia-smi -i "$idx" --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1)
  mem=$(nvidia-smi -i "$idx" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n 1 | tr -d ' ')
  drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1 | cut -d. -f1)
  if [ "${mem:-0}" -ge 45000 ]; then ok "GPU $idx：$name，${mem} MiB"; else bad "GPU $idx 显存不足 45GB：$name ${mem:-?} MiB"; fi
  printf '%s' "$name" | grep -q A6000 || warn "GPU $idx 不是 RTX A6000（$name），批量参数按 A6000 调的"
  if [ "${drv:-0}" -ge 525 ]; then ok "驱动 $drv（CUDA 11.8 / 12.1 都能跑）"; else bad "驱动 $drv 太旧，至少 525"; fi
else
  bad "没有 nvidia-smi：先装 NVIDIA 驱动"
fi

if docker compose version >/dev/null 2>&1; then ok "$(docker compose version | head -n 1)"; else bad "没有 docker compose v2 插件"; fi
if docker run --rm --gpus "device=${GPU_DEVICE:-0}" nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi -L >/dev/null 2>&1; then
  ok "容器内能看到 GPU（nvidia-container-toolkit 正常）"
else
  bad "容器里用不了 GPU：装 nvidia-container-toolkit 后执行 nvidia-ctk runtime configure --runtime=docker && systemctl restart docker"
fi

need_dir() {
  local label="$1" path="${2:-}" mode="$3"
  [ -n "$path" ] || { warn "$label 未设置，跳过"; return; }
  if [ ! -d "$path" ]; then bad "$label 不存在：$path"; return; fi
  if [ "$mode" = r ] && [ -z "$(ls -A "$path" 2>/dev/null | head -n 1)" ]; then bad "$label 是空目录（没挂载？）：$path"; return; fi
  ok "$label：$path（剩余 $(df -h --output=avail "$path" | tail -n 1 | tr -d ' ')）"
}
need_dir SOURCE_ROOT "${SOURCE_ROOT:-}" r
need_dir ARCHIVE_ROOT "${ARCHIVE_ROOT:-}" w
need_dir DATA_DIR "${DATA_DIR:-}" w
need_dir PG_DATA_DIR "${PG_DATA_DIR:-}" w
need_dir MODEL_DIR "${MODEL_DIR:-}" w

if [ -n "${MODEL_DIR:-}" ]; then
  [ -d "$MODEL_DIR/paddleocr/whl/rec" ] && ok "OCR 模型已下载" || warn "OCR 模型还没下载：./ops.sh fetch-models"
  [ -d "$MODEL_DIR/hf/hub/models--BAAI--bge-m3" ] && ok "bge-m3 已下载" || warn "bge-m3 还没下载：./ops.sh fetch-models"
fi
if [ -n "${SOURCE_ROOT:-}" ] && [ -n "${ARCHIVE_ROOT:-}" ]; then
  case "$ARCHIVE_ROOT/" in "$SOURCE_ROOT"/*) bad "ARCHIVE_ROOT 不能放在 SOURCE_ROOT 里面（源目录是只读挂载）";; esac
fi

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
