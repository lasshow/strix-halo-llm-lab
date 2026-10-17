#!/bin/bash
# Despliega Gufo como motor de llama-flashnext.service (drop-in 60-gufo.conf).
# Si no queda sano en 5 min, revierte a llama.cpp. Uso: despliega-gufo.sh <ruta-60-gufo.conf>
set -u
SRC=$1
DIR=/etc/systemd/system/llama-flashnext.service.d
DST=$DIR/60-gufo.conf
OUT=~/campanas/despliegue-gufo-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$OUT"
log(){ echo "[$(date +%F' '%T)] $*" | tee -a "$OUT/despliegue.log"; }

sano(){ # /health 200 y el modelo servido es el de produccion (Gufo con --api-key exige la clave tambien en /health)
  local k; k=$(grep -v -E '^\s*(#|$)' /etc/llama-server/api-keys.txt | head -1 | tr -d '[:space:]')
  curl -sf -o /dev/null -H "Authorization: Bearer $k" http://127.0.0.1:8080/health &&
  curl -sf -H "Authorization: Bearer $k" http://127.0.0.1:8080/v1/models | grep -q '"qwen3.8-flash-next"'
}

revierte(){
  log "REVIRTIENDO a llama.cpp"
  sudo rm -f "$DST"; sudo systemctl daemon-reload
  podman rm -f gufo-flashnext >/dev/null 2>&1
  sudo systemctl restart llama-flashnext
  for i in $(seq 1 120); do curl -sf -o /dev/null http://127.0.0.1:8080/health && { log "llama.cpp de vuelta"; return; }; sleep 5; done
  log "AVISO: llama.cpp tampoco responde"
}

[ -e /etc/llama-guard.disable ] && { log "ABORTO: /etc/llama-guard.disable existe (otra sesion usa la GPU)"; exit 1; }
[ "$(ps -eo args | grep -c '/[b]rush_cli')" = 0 ] || { log "ABORTO: hay un entrenamiento de brush"; exit 1; }

log "antes: $(systemctl show llama-flashnext -p ExecStart --value | grep -o 'path=[^ ;]*')"
sudo systemctl stop llama-guard.timer
sudo install -m 0644 -o root -g root "$SRC" "$DST"
sudo systemctl daemon-reload
log "drop-in instalado; reiniciando llama-flashnext"
t0=$(date +%s)
sudo systemctl restart llama-flashnext
ok=0
for i in $(seq 1 60); do
  sano && { ok=1; break; }
  systemctl is-failed --quiet llama-flashnext && break
  sleep 5
done
if [ $ok -ne 1 ]; then
  log "Gufo no quedo sano en $(( $(date +%s) - t0 )) s"
  sudo journalctl -u llama-flashnext -n 60 --no-pager > "$OUT/journal-fallo.txt" 2>&1
  revierte; sudo systemctl start llama-guard.timer; exit 1
fi
log "Gufo sano en $(( $(date +%s) - t0 )) s"
podman ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep gufo-flashnext | tee -a "$OUT/despliegue.log"
podman logs gufo-flashnext 2>&1 | grep -E 'load_completed|listening' | tee -a "$OUT/despliegue.log"
sudo systemctl start llama-guard.timer
log "guard reactivado; FIN"
