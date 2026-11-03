#!/bin/bash
# Campana DS4.1: Gufo IQ1_M (+DSpark / sin) vs Strata. Restaura Qwen SIEMPRE al salir.
set -u
D=${BENCH_DIR:-$HOME/ds41-bench}; cd "$D"; OUT=$D/resultados.jsonl; LOG=$D/campana.log
M=/models/gguf/ds41-gufo
export XDG_RUNTIME_DIR=/run/user/$(id -u)
STRATA_KEY=$(python3 -c "import json;print(json.load(open('/models/strata-lab/strata/cfg-servir.json'))['api_key'])")

restaurar() {
  echo "== restaurar $(date +%T)"
  podman rm -f gufo-ds41 >/dev/null 2>&1
  sudo -n m5-model qwen
  sudo -n m5-model status
  sudo -n systemctl stop ds41-deadman.timer 2>/dev/null
  echo "== FIN $(date +%T)"
}
trap restaurar EXIT

arranca_gufo() {  # $1 = extra args
  podman rm -f gufo-ds41 >/dev/null 2>&1
  podman run -d --rm --name gufo-ds41 --device /dev/kfd --device /dev/dri --group-add keep-groups \
    --ipc=host --ulimit memlock=-1 --security-opt label=disable -p 127.0.0.1:8087:8080 \
    -v gufo-nix:/nix:ro -v /models:/models:ro docker.io/nixos/nix:latest \
    /nix/var/gufo/bin/gufo serve llm --host 0.0.0.0 --port 8080 --served-model-name ds41 \
    -m $M/DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ1_M-00001-of-00006.gguf --context 131072 "$@" >/dev/null
  local t0=$(date +%s)
  for _ in $(seq 1 180); do
    c=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8087/health)
    [ "$c" = 200 ] && { echo "gufo listo en $(( $(date +%s)-t0 )) s; free:"; free -g | sed -n 2p; return 0; }
    podman ps -q -f name=gufo-ds41 | grep -q . || { echo "gufo MUERTO"; podman logs gufo-ds41 2>&1 | tail -20; return 1; }
    sleep 5
  done
  echo "gufo no arranco en 15 min"; podman logs gufo-ds41 2>&1 | tail -20; return 1
}

{
echo "== INICIO $(date +%T)"
sudo -n touch /etc/llama-guard.disable
sudo -n systemctl stop llama-flashnext llama-uncensored
podman rm -f gufo-flashnext gufo-uncensored >/dev/null 2>&1
sleep 5; free -g | sed -n 2p

echo "== BRAZO gufo-dspark"
if arranca_gufo --speculative dspark --dspark-model $M/DeepSeek-V4.1-Flash-1017-DSpark-draft-Q2K-Q4K.gguf; then
  python3 h057-cliente-stream.py gufo-dspark http://127.0.0.1:8087 - ds41 "$OUT"
  podman logs gufo-ds41 2>&1 | tail -40 > gufo-dspark.log
fi
podman stop -t 30 gufo-ds41 >/dev/null 2>&1; podman rm -f gufo-ds41 >/dev/null 2>&1; sleep 10

echo "== BRAZO gufo-plain"
if arranca_gufo; then
  python3 h057-cliente-stream.py gufo-plain http://127.0.0.1:8087 - ds41 "$OUT"
  podman logs gufo-ds41 2>&1 | tail -40 > gufo-plain.log
fi
podman stop -t 30 gufo-ds41 >/dev/null 2>&1; podman rm -f gufo-ds41 >/dev/null 2>&1; sleep 10

if [ -z "${SOLO_GUFO:-}" ]; then echo "== BRAZO strata"
sudo -n m5-model deepseek
python3 h057-cliente-stream.py strata http://127.0.0.1:8085 "$STRATA_KEY" deepseek-v4.1-flash "$OUT"; fi
} >> "$LOG" 2>&1
