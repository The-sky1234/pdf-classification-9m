#!/usr/bin/env bash
# GPU环境检测：nvidia驱动 / CUDA / 容器运行时 / 深度学习框架 / MinerU
# 用法：bash gpu_check.sh；退出码0=全部通过，1=有未满足项（输出[FAIL]行）
set -u
pass=0; fail=0
ok(){ echo "[OK] $1"; pass=$((pass+1)); }
bad(){ echo "[FAIL] $1"; fail=$((fail+1)); }
command -v nvidia-smi >/dev/null 2>&1 && { nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv; ok "nvidia-smi"; } || bad "nvidia-smi缺失：安装NVIDIA驱动"
if command -v nvidia-smi >/dev/null 2>&1; then
  gpu_n=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l | tr -d ' ')
  gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n 1)
  gpu_mem=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n 1 | tr -d ' ')
  if [ "$gpu_n" = "1" ] && printf '%s' "$gpu_name" | grep -q 'A6000' && [ "$gpu_mem" -ge 45000 ]; then
    ok "唯一GPU是RTX A6000 48GB（${gpu_mem} MiB）"
  else
    bad "GPU必须写死为1张RTX A6000且显存达到48GB，当前数量=${gpu_n} 名称=${gpu_name} 显存MiB=${gpu_mem}"
  fi
fi
command -v nvcc >/dev/null 2>&1 && { nvcc --version | tail -n 1; ok "nvcc"; } || bad "nvcc缺失（仅检查，可用容器内CUDA）"
docker info 2>/dev/null | grep -qi nvidia && ok "docker nvidia runtime" || bad "docker nvidia-container-toolkit未配置"
for mod in torch paddle FlagEmbedding sentence_transformers magic_pdf; do
  python3 -c "import $mod" 2>/dev/null && ok "py:$mod" || bad "py:$mod 缺失"
done
python3 - <<'EOF'
try:
    import torch
    print("torch", torch.__version__, "cuda_avail=", torch.cuda.is_available(), "gpus=", torch.cuda.device_count() if torch.cuda.is_available() else 0)
except Exception as e:
    print("torch检查失败:", e)
EOF
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
