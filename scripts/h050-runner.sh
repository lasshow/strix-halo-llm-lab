#!/bin/bash
# H-050: tres conversaciones largas intercaladas contra 2 slots, llama.cpp frente a
# Gufo 0.17.0 (mismas opciones que en H-049). Produccion se restaura al salir.
set -u
SNAP=$1
OUT=~/campanas/h050-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"
IMG=ghcr.io/gufo-org/toolboxes/gufo-runtime:latest
MOD=/models/gguf/qwen38-flash-next
CT=gufo-h050
log(){ echo "[$(date +%F' '%T)] $*" | tee -a "$OUT/runner.log"; }
espera_llama(){ local i=0; while [ $i -lt 900 ]; do curl -sf -o /dev/null http://127.0.0.1:8080/health && return 0; sleep 5; i=$((i+5)); done; return 1; }

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
sudo systemctl stop llama-guard.timer

log "toma llama.cpp (reinicio limpio para partir sin cache)"
sudo systemctl restart llama-flashnext.service
espera_llama || { log "ERROR: llama-flashnext no arranca"; exit 1; }
MOTOR_KEY=""
while IFS= read -r l; do
  l=$(echo "$l" | tr -d '[:space:]'); [ -z "$l" ] && continue; case $l in \#*) continue;; esac
  if curl -sf -o /dev/null -H "Authorization: Bearer $l" http://127.0.0.1:8080/v1/models; then MOTOR_KEY=$l; break; fi
done < /etc/llama-server/api-keys.txt
MOTOR_KEY=$MOTOR_KEY python3 "$SNAP/scripts/h050-afinidad.py" --base http://127.0.0.1:8080/v1 \
  --model qwen3.8-flash-next --etiqueta llamacpp --doc "$SNAP/docs/hallazgos.md" \
  --salida "$OUT/llamacpp.jsonl" > "$OUT/llamacpp.txt" 2>&1
log "  rc=$?"

log "toma Gufo"
sudo systemctl stop llama-flashnext.service
sleep 15
podman run -d --name $CT --device /dev/kfd --device /dev/dri --group-add keep-groups \
  --ulimit memlock=-1 --security-opt label=disable -v /models:/models:ro \
  -p 127.0.0.1:8081:8081 "$IMG" \
  gufo serve --host 0.0.0.0 --port 8081 --sessions 2 llm \
  -m "$MOD/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf" \
  --mmproj "$MOD/mmproj-BF16.gguf" \
  --mtp-model "$MOD/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf" --speculative mtp \
  --context 262144 --cache-ram-bytes 8589934592 \
  --served-model-name "Qwen3.8 Flash Next" --think off >> "$OUT/runner.log" 2>&1
listo=0
for i in $(seq 1 300); do
  curl -sf http://127.0.0.1:8081/v1/models 2>/dev/null | grep -q '"id"' && { listo=1; break; }
  [ "$(podman inspect -f '{{.State.Running}}' $CT 2>/dev/null)" = "true" ] || break
  sleep 5
done
[ $listo -eq 1 ] || { log "ERROR: Gufo no arranca"; exit 1; }
python3 "$SNAP/scripts/h050-afinidad.py" --base http://127.0.0.1:8081/v1 \
  --model "Qwen3.8 Flash Next" --etiqueta gufo --doc "$SNAP/docs/hallazgos.md" \
  --salida "$OUT/gufo.jsonl" > "$OUT/gufo.txt" 2>&1
log "  rc=$?"
