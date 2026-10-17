#!/bin/bash
# H-049b: Gufo 0.17.0 con --draft-tokens 3 y 2 (el defecto es 7), banco de carga real.
# Mismas opciones que H-049 salvo esa. Al terminar encadena H-050.
set -u
SNAP=$1
OUT=~/campanas/h049b-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"
IMG=ghcr.io/gufo-org/toolboxes/gufo-runtime:latest
MOD=/models/gguf/qwen38-flash-next
CT=gufo-h049b
log(){ echo "[$(date +%F' '%T)] $*" | tee -a "$OUT/runner.log"; }
espera_llama(){ local i=0; while [ $i -lt 900 ]; do curl -sf -o /dev/null http://127.0.0.1:8080/health && return 0; sleep 5; i=$((i+5)); done; return 1; }
restaura(){
  podman rm -f $CT >/dev/null 2>&1
  sudo systemctl start llama-flashnext.service
  sudo systemctl start llama-guard.timer
  espera_llama && log "produccion OK" || log "AVISO: llama-flashnext sin /health"
  log "FIN (salida en $OUT)"
}
trap restaura EXIT
sudo systemctl stop llama-guard.timer
sudo systemctl stop llama-flashnext.service
sleep 15
for d in 3 2; do
  log "draft-tokens $d"
  podman rm -f $CT >/dev/null 2>&1
  podman run -d --name $CT --device /dev/kfd --device /dev/dri --group-add keep-groups \
    --ulimit memlock=-1 --security-opt label=disable -v /models:/models:ro \
    -p 127.0.0.1:8081:8081 "$IMG" \
    gufo serve --host 0.0.0.0 --port 8081 --sessions 2 llm \
    -m "$MOD/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf" \
    --mmproj "$MOD/mmproj-BF16.gguf" \
    --mtp-model "$MOD/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf" --speculative mtp \
    --draft-tokens $d --context 262144 --cache-ram-bytes 8589934592 \
    --served-model-name "Qwen3.8 Flash Next" --think off >> "$OUT/runner.log" 2>&1
  listo=0
  for i in $(seq 1 300); do
    curl -sf http://127.0.0.1:8081/v1/models 2>/dev/null | grep -q '"id"' && { listo=1; break; }
    [ "$(podman inspect -f '{{.State.Running}}' $CT 2>/dev/null)" = "true" ] || break
    sleep 5
  done
  [ $listo -eq 1 ] || { log "ERROR: Gufo no arranca con draft $d"; continue; }
  python3 "$SNAP/scripts/h049-bench-real-gufo.py" --url http://127.0.0.1:8081 --contenedor $CT \
    --pasadas 2 --salida "$OUT/real-gufo-d$d.jsonl" > "$OUT/real-gufo-d$d.txt" 2>&1
  log "  rc=$?"
  podman logs $CT > "$OUT/gufo-d$d.server.txt" 2>&1
done
