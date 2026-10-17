#!/bin/bash
# H-049: Gufo actualizado (0.9.1 -> ultima) como candidato a motor de produccion.
# Toma Gufo (GPU en exclusiva): carga con el contexto de produccion, banco de carga
# real, bateria de calidad H-047 y pruebas de aptitud (fugas, concurrencia, vision,
# herramientas). Toma llama.cpp: las mismas pruebas de aptitud como referencia.
# Produccion se restaura SIEMPRE al salir (trap).
set -u
SNAP=$1
OUT=~/campanas/h049-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"
IMG=ghcr.io/gufo-org/toolboxes/gufo-runtime
MOD=/models/gguf/qwen38-flash-next
CT=gufo-h049
log(){ echo "[$(date +%F' '%T)] $*" | tee -a "$OUT/runner.log"; }

espera_llama(){
  local i=0
  while [ $i -lt 900 ]; do
    curl -sf -o /dev/null http://127.0.0.1:8080/health && return 0
    sleep 5; i=$((i+5))
  done
  return 1
}

restaura(){
  log "restaurando produccion"
  podman logs $CT > "$OUT/gufo.server.txt" 2>&1
  podman rm -f $CT >/dev/null 2>&1
  sudo systemctl start llama-flashnext.service
  sudo systemctl start llama-guard.timer
  espera_llama && log "produccion OK" || log "AVISO: llama-flashnext sin /health"
  log "FIN (salida en $OUT)"
}
trap restaura EXIT

log "pull de $IMG:latest"
podman pull -q "$IMG:latest" >> "$OUT/runner.log" 2>&1
log "imagen: $(podman image inspect $IMG:latest --format '{{.Digest}}')"
log "version: $(podman run --rm $IMG:latest gufo --version 2>&1 | head -1)"

sudo systemctl stop llama-guard.timer
sudo systemctl stop llama-flashnext.service
sleep 15
log "MemAvailable antes de Gufo: $(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo) MB"

arranca_gufo(){ # $1 = contexto por sesion
  podman rm -f $CT >/dev/null 2>&1
  podman run -d --name $CT --device /dev/kfd --device /dev/dri --group-add keep-groups \
    --ulimit memlock=-1 --security-opt label=disable -v /models:/models:ro \
    -p 127.0.0.1:8081:8081 "$IMG:latest" \
    gufo serve --host 0.0.0.0 --port 8081 --sessions 2 llm \
    -m "$MOD/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf" \
    --mmproj "$MOD/mmproj-BF16.gguf" \
    --mtp-model "$MOD/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf" --speculative mtp \
    --context "$1" --cache-ram-bytes 8589934592 \
    --served-model-name "Qwen3.8 Flash Next" --think off >> "$OUT/runner.log" 2>&1
  local i
  for i in $(seq 1 300); do
    curl -sf http://127.0.0.1:8081/v1/models 2>/dev/null | grep -q '"id"' && return 0
    [ "$(podman inspect -f '{{.State.Running}}' $CT 2>/dev/null)" = "true" ] || return 1
    sleep 5
  done
  return 1
}

CTX=262144
if ! arranca_gufo $CTX; then
  log "Gufo no arranca con 2 x $CTX; log:"; podman logs --tail 15 $CT >> "$OUT/runner.log" 2>&1
  CTX=131072
  arranca_gufo $CTX || { log "ERROR: Gufo no arranca ni con 2 x $CTX"; exit 1; }
fi
log "Gufo arriba con --sessions 2 --context $CTX"
podman logs $CT 2>&1 | grep -E "load_completed|listening" >> "$OUT/runner.log"
log "MemAvailable con Gufo: $(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo) MB"

log "banco de carga real (Gufo)"
python3 "$SNAP/scripts/h049-bench-real-gufo.py" --url http://127.0.0.1:8081 --contenedor $CT \
  --pasadas 2 --salida "$OUT/real-gufo.jsonl" > "$OUT/real-gufo.txt" 2>&1
log "  rc=$?"
log "bateria de calidad H-047 (Gufo)"
python3 "$SNAP/scripts/h047-mismo-gguf.py" --base http://127.0.0.1:8081/v1 \
  --model "Qwen3.8 Flash Next" --etiqueta gufo --pasadas 3 \
  --salida "$OUT/calidad-gufo.jsonl" > "$OUT/calidad-gufo.txt" 2>&1
log "  rc=$?"
log "pruebas de aptitud (Gufo)"
python3 "$SNAP/scripts/h049-pruebas.py" --base http://127.0.0.1:8081/v1 \
  --model "Qwen3.8 Flash Next" --etiqueta gufo --salida "$OUT/aptitud-gufo.json" \
  > "$OUT/aptitud-gufo.txt" 2>&1
log "  rc=$?"
log "MemAvailable al final de Gufo: $(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo) MB"
podman logs $CT > "$OUT/gufo.server.txt" 2>&1
podman rm -f $CT >/dev/null 2>&1

log "toma de referencia: llama.cpp de produccion"
sudo systemctl start llama-flashnext.service
espera_llama || { log "ERROR: llama-flashnext no arranca"; exit 1; }
MOTOR_KEY=""
while IFS= read -r l; do
  l=$(echo "$l" | tr -d '[:space:]'); [ -z "$l" ] && continue; case $l in \#*) continue;; esac
  if curl -sf -o /dev/null -H "Authorization: Bearer $l" http://127.0.0.1:8080/v1/models; then MOTOR_KEY=$l; break; fi
done < /etc/llama-server/api-keys.txt
MOTOR_KEY=$MOTOR_KEY python3 "$SNAP/scripts/h049-pruebas.py" --base http://127.0.0.1:8080/v1 \
  --model qwen3.8-flash-next --etiqueta llamacpp --salida "$OUT/aptitud-llamacpp.json" \
  > "$OUT/aptitud-llamacpp.txt" 2>&1
log "  rc=$?"
