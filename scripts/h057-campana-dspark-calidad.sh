#!/bin/bash
# H-057 ronda 2: DSpark con contexto reducido + bateria de calidad Gufo IQ1_M vs Strata.
set -u
D=${BENCH_DIR:-$HOME/ds41-bench}; cd "$D"; OUT=$D/resultados.jsonl; LOG=$D/campana3.log
M=/models/gguf/ds41-gufo
export XDG_RUNTIME_DIR=/run/user/$(id -u)
STRATA_KEY=$(python3 -c "import json;print(json.load(open('/models/strata-lab/strata/cfg-servir.json'))['api_key'])")
restaurar() { echo "== restaurar $(date +%T)"; podman rm -f gufo-ds41 >/dev/null 2>&1; sudo -n m5-model qwen; sudo -n m5-model status; sudo -n systemctl stop ds41-deadman.timer 2>/dev/null; echo "== FIN $(date +%T)"; }
trap restaurar EXIT
arranca() {  # $1 etiqueta, resto args extra
  local et=$1; shift
  podman rm -f gufo-ds41 >/dev/null 2>&1
  podman run -d --name gufo-ds41 --device /dev/kfd --device /dev/dri --group-add keep-groups \
    --ipc=host --ulimit memlock=-1 --security-opt label=disable -p 127.0.0.1:8087:8080 \
    -v gufo-nix:/nix:ro -v /models:/models:ro docker.io/nixos/nix:latest \
    /nix/var/gufo/bin/gufo serve llm --host 0.0.0.0 --port 8080 --served-model-name ds41 \
    -m $M/DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ1_M-00001-of-00006.gguf "$@" >/dev/null
  local t0=$(date +%s)
  for _ in $(seq 1 180); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8087/health)" = 200 ] && { echo "[$et] listo en $(( $(date +%s)-t0 )) s"; free -g | sed -n 2p; return 0; }
    if [ "$(podman inspect -f '{{.State.Running}}' gufo-ds41 2>/dev/null)" != true ]; then
      echo "[$et] MUERTO:"; podman logs gufo-ds41 2>&1 | grep -E "needs|Error|error" | head -4; podman rm -f gufo-ds41 >/dev/null 2>&1; return 1; fi
    sleep 5
  done; echo "[$et] no arranco"; return 1
}
para() { podman logs gufo-ds41 > "gufo-$1.log" 2>&1; podman stop -t 30 gufo-ds41 >/dev/null 2>&1; podman rm -f gufo-ds41 >/dev/null 2>&1; sleep 10; }
{
echo "== INICIO $(date +%T)"
sudo -n touch /etc/llama-guard.disable
sudo -n systemctl stop llama-flashnext llama-uncensored; podman rm -f gufo-flashnext gufo-uncensored >/dev/null 2>&1
sudo -n pkill -x strata-dsv41 2>/dev/null; sleep 5; free -g | sed -n 2p
DS="--speculative dspark --dspark-model $M/DeepSeek-V4.1-Flash-1017-DSpark-draft-Q2K-Q4K.gguf"
for ctx in 65536 32768; do
  if arranca dspark-$ctx --context $ctx --cache-ram-bytes off $DS; then
    python3 h057-cliente-stream.py gufo-dspark-$ctx http://127.0.0.1:8087 - ds41 "$OUT"; para dspark-$ctx; break
  fi
done
if arranca calidad-gufo --context 131072 --think off; then
  python3 bench-calidad-motores.py --base http://127.0.0.1:8087/v1 --model ds41 --etiqueta gufo-iq1m --salida cal-gufo-iq1m.jsonl --bust-cache
  para calidad-gufo
fi
echo "== STRATA calidad"
sudo -n m5-model deepseek
MOTOR_KEY="$STRATA_KEY" python3 bench-calidad-motores.py --base http://127.0.0.1:8085/v1 --model deepseek-v4.1-flash --etiqueta strata --salida cal-strata.jsonl --bust-cache
} >> "$LOG" 2>&1
