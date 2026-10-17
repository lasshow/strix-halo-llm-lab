#!/bin/bash
# H-045: celdas dudosas de H-044 con muestreo greedy igualado y 3 pasadas.
# Toma A = llama-flashnext de produccion; toma B = Gufo 0.9.1 con la GPU en exclusiva.
# Produccion se restaura SIEMPRE al salir (trap).
set -u
SCR=~/lab/scripts
OUT=~/campanas/h045-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"
IMG=ghcr.io/gufo-org/toolboxes/gufo-runtime
MOD=/models/gguf/qwen38-flash-next
log(){ echo "[$(date +%F' '%T)] $*" | tee -a "$OUT/runner.log"; }

espera(){ # url, segundos max, cabecera opcional
  local url=$1 max=$2 i=0
  while [ $i -lt "$max" ]; do
    if curl -sf -o /dev/null ${3:+-H "$3"} "$url"; then return 0; fi
    sleep 5; i=$((i+5))
  done
  return 1
}

restaura(){
  log "restaurando produccion"
  podman rm -f gufo-h045 >/dev/null 2>&1
  sudo systemctl start llama-flashnext.service
  sudo systemctl start llama-guard.timer
  if espera http://127.0.0.1:8080/health 900; then log "produccion OK"; else log "AVISO: llama-flashnext sin /health tras 15 min"; fi
  log "FIN (salida en $OUT)"
}
trap restaura EXIT

sudo systemctl stop llama-guard.timer
log "guard parado; kernel $(uname -r); build $(readlink /models/llama-current)"

# Clave de produccion: primera linea util de api-keys.txt; no se imprime nunca.
MOTOR_KEY=""
while IFS= read -r l; do
  l=$(echo "$l" | tr -d '[:space:]'); [ -z "$l" ] && continue; case $l in \#*) continue;; esac
  if curl -sf -o /dev/null -H "Authorization: Bearer $l" http://127.0.0.1:8080/v1/models; then MOTOR_KEY=$l; break; fi
done < /etc/llama-server/api-keys.txt
[ -z "$MOTOR_KEY" ] && { log "ERROR: ninguna clave valida para :8080"; exit 1; }

log "toma A: llama.cpp"
MOTOR_KEY=$MOTOR_KEY python3 "$SCR/h045-celdas-dudosas.py" --base http://127.0.0.1:8080/v1 \
  --model qwen3.8-flash-next --etiqueta llamacpp --pasadas 3 \
  --salida "$OUT/llamacpp.jsonl" > "$OUT/llamacpp.salida.txt" 2>&1
log "toma A terminada (rc=$?)"

log "parando llama-flashnext para dar la GPU en exclusiva a Gufo"
sudo systemctl stop llama-flashnext.service
sleep 20
podman run -d --name gufo-h045 --device /dev/kfd --device /dev/dri --group-add keep-groups \
  --security-opt label=disable -v /models:/models:ro -p 127.0.0.1:8081:8081 "$IMG" \
  gufo serve --host 0.0.0.0 --port 8081 --sessions 1 llm \
  -m "$MOD/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf" \
  --mtp-model "$MOD/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf" --speculative mtp \
  --context 131072 --served-model-name "Qwen3.8 Flash Next" --think off >> "$OUT/runner.log" 2>&1
if ! espera http://127.0.0.1:8081/v1/models 1500; then
  log "ERROR: Gufo no responde en 25 min"; podman logs --tail 80 gufo-h045 > "$OUT/gufo.server.txt" 2>&1; exit 1
fi
log "toma B: gufo"
python3 "$SCR/h045-celdas-dudosas.py" --base http://127.0.0.1:8081/v1 \
  --model "Qwen3.8 Flash Next" --etiqueta gufo --pasadas 3 \
  --salida "$OUT/gufo.jsonl" > "$OUT/gufo.salida.txt" 2>&1
log "toma B terminada (rc=$?)"
podman logs gufo-h045 > "$OUT/gufo.server.txt" 2>&1
