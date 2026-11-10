#!/bin/bash
# H-063: (1) Gufo Qwen, variantes de servidor + gufo bench; (2) DeepSeek Gufo vs Strata v0.2.13.1
cd /models/strata-lab
L=h063.log; O=vel-h063.jsonl
log(){ echo "$(date +%T) $*" | tee -a $L; }
IMG=$(sudo -n systemctl show llama-flashnext -p Environment | tr ' ' '\n' | sed -n 's/^GUFO_IMG=//p')
M=/models/gguf/qwen38-flash-next
GG="$M/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf"
MTP="$M/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf"
PR="podman run --rm --device /dev/kfd --device /dev/dri --group-add keep-groups --ulimit memlock=-1 --security-opt label=disable -v /models:/models:ro"
CASOS=codigo-512,prosa-512,prefill-32k

sudo -n touch /etc/llama-guard.disable
sudo -n systemctl stop llama-swap llama-flashnext
log "produccion parada; imagen $IMG"

# --- gufo bench sin servidor (comparable con la tabla publicada: pp2048 tg128)
log "== gufo bench AR"
$PR $IMG gufo bench -m $GG -p 2048 -n 128 -r 3 >> $L 2>&1
log "== gufo bench MTP"
$PR $IMG gufo bench -m $GG -p 2048 -n 128 -r 3 --speculative mtp --mtp-model $MTP >> $L 2>&1

variante(){ # etiqueta  opciones-globales  opciones-llm
  log "== $1 ($2 | $3)"
  podman rm -f gufo-h063 >/dev/null 2>&1
  $PR --name gufo-h063 -d -p 127.0.0.1:18081:8080 $IMG gufo serve --host 0.0.0.0 --port 8080 $2 llm \
     -m $GG --mmproj $M/mmproj-BF16.gguf --mtp-model $MTP --speculative mtp \
     --context 262144 --cache-ram-bytes 4294967296 --served-model-name qwen3.8-flash-next $3 >/dev/null
  for i in $(seq 1 90); do curl -s -m3 -o /dev/null -w "%{http_code}" 127.0.0.1:18081/health | grep -q 200 && break; sleep 5; done
  python3 bench_vel.py --base http://127.0.0.1:18081/v1 --etiqueta "$1" --salida $O --reps 3 --casos $CASOS >> $L 2>&1
  podman stop -t 30 gufo-h063 >/dev/null 2>&1; podman rm -f gufo-h063 >/dev/null 2>&1
}
variante q-prod-s2   "--sessions 2" ""
variante q-s1        "--sessions 1" ""
variante q-s2-d4     "--sessions 2" "--draft-tokens 4"
variante q-s2-min3   "--sessions 2" "--min-draft-tokens 3"

# --- DeepSeek
DSKEY=$(sudo -n tr -d '[:space:]' < /etc/llama-server/ds41.key)
STKEY=$(python3 -c 'import json;print(json.load(open("/models/strata-lab/strata/cfg-servir.json")).get("api_key",""))')
DCASOS=codigo-512,prosa-512,prefill-10k
/usr/local/bin/m5-model deepseek >> $L 2>&1
log "== ds-gufo"
python3 bench_vel.py --base http://127.0.0.1:18085/v1 --model deepseek-v4.1-flash --key "$DSKEY" --etiqueta ds-gufo --salida $O --reps 3 --casos $DCASOS >> $L 2>&1
/usr/local/bin/m5-model strata >> $L 2>&1
log "== ds-strata-0213"
python3 bench_vel.py --base http://127.0.0.1:8085/v1 --model deepseek-v4.1-flash --key "$STKEY" --etiqueta ds-strata-0213 --salida $O --reps 3 --casos $DCASOS >> $L 2>&1

/usr/local/bin/m5-model qwen >> $L 2>&1
sudo -n rm -f /etc/llama-guard.disable
sudo -n systemctl start llama-swap
sudo -n systemctl stop h063-deadman.timer 2>/dev/null
log "FIN restaurado: $(systemctl is-active llama-swap llama-flashnext | tr '\n' ' ')"
