#!/bin/bash
# H-050b: ¿Gufo 0.17.0 acepta VARIAS claves con --api-key repetido, o solo la ultima?
# Claves ficticias (no las de produccion). Codigo HTTP con cada clave y sin clave,
# en /v1/models y en /v1/chat/completions. Produccion se restaura al salir.
set -u
OUT=~/campanas/h050b-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$OUT"
IMG=ghcr.io/gufo-org/toolboxes/gufo-runtime@sha256:a3df87bca3d33aef3df6f5c509ee54f743c0cb6e2cb5dbc83f2ebac2289349d2
MOD=/models/gguf/qwen38-flash-next
CT=gufo-h050b
log(){ echo "[$(date +%F' '%T)] $*" | tee -a "$OUT/runner.log"; }
restaura(){
  podman rm -f $CT >/dev/null 2>&1
  sudo systemctl start llama-flashnext.service; sudo systemctl start llama-guard.timer
  for i in $(seq 1 180); do curl -sf -o /dev/null http://127.0.0.1:8080/health && { log "produccion OK"; break; }; sleep 5; done
  log "FIN"
}
trap restaura EXIT
sudo systemctl stop llama-guard.timer; sudo systemctl stop llama-flashnext.service; sleep 10
podman run -d --name $CT --device /dev/kfd --device /dev/dri --group-add keep-groups \
  --ulimit memlock=-1 --security-opt label=disable -v /models:/models:ro -p 127.0.0.1:8081:8081 "$IMG" \
  gufo serve --host 0.0.0.0 --port 8081 --sessions 2 --api-key clave-prueba-a --api-key clave-prueba-b llm \
  -m $MOD/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf --mmproj $MOD/mmproj-BF16.gguf \
  --mtp-model $MOD/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf --speculative mtp \
  --context 262144 --cache-ram-bytes 4294967296 --served-model-name qwen3.8-flash-next > /dev/null
for i in $(seq 1 120); do
  c=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer clave-prueba-b" http://127.0.0.1:8081/v1/models)
  [ "$c" = 200 ] || [ "$c" = 401 ] && break; sleep 3
done
for k in clave-prueba-a clave-prueba-b mala ""; do
  cab=(); [ -n "$k" ] && cab=(-H "Authorization: Bearer $k")
  m=$(curl -s -o /dev/null -w '%{http_code}' "${cab[@]}" http://127.0.0.1:8081/v1/models)
  c=$(curl -s -o /dev/null -w '%{http_code}' "${cab[@]}" -H 'Content-Type: application/json' \
      -d '{"model":"qwen3.8-flash-next","messages":[{"role":"user","content":"di hola"}],"max_tokens":4}' \
      http://127.0.0.1:8081/v1/chat/completions)
  log "clave='${k:-<ninguna>}'  /v1/models=$m  /v1/chat/completions=$c"
done
podman logs $CT 2>&1 | grep -E "listening|auth" | head -3 >> "$OUT/runner.log"
