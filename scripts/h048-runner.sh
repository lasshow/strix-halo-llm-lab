#!/bin/bash
# H-048: reloj de GPU fijo (power_dpm_force_performance_level=high) y carga sin mmap
# (LLAMA_ARG_LOAD_MODE=none; esta build no tiene el modo "dio" del fork de drluoto),
# medidos con el banco de carga real contra el servidor de produccion.
#   A = base (auto, mmap)   B = reloj high   C = reloj high + load-mode none
# Orden A B C C B A para compensar deriva termica. Una variable por salto.
# Al salir deja produccion en la configuracion BASE (la adopcion se hace aparte).
set -u
SNAP=$1                      # copia congelada del repo (scripts/, docs/, README.md)
OUT=~/campanas/h048-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT" "$SNAP/benchmarks"
DPM=/sys/class/drm/card0/device/power_dpm_force_performance_level
DROP=/etc/systemd/system/llama-flashnext.service.d/h048-loadmode.conf
log(){ echo "[$(date +%F' '%T)] $*" | tee -a "$OUT/runner.log"; }

espera_salud(){
  local i=0
  while [ $i -lt 900 ]; do
    curl -sf -o /dev/null http://127.0.0.1:8080/health && return 0
    systemctl is-failed --quiet llama-flashnext && return 1
    sleep 5; i=$((i+5))
  done
  return 1
}

vigila_memoria(){ # para el servicio si la memoria disponible baja de 1,5 GB
  while true; do
    av=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
    if [ "$av" -lt 1536 ]; then
      log "VIGILANTE: MemAvailable=${av} MB, paro llama-flashnext"
      sudo systemctl stop llama-flashnext; break
    fi
    sleep 2
  done
}

muestrea_reloj(){ # $1 = fichero; SCLK real cada 2 s
  while true; do
    echo "$(date +%T) $(sudo cat /sys/kernel/debug/dri/0/amdgpu_pm_info 2>/dev/null | grep -E '\((MCLK|SCLK)\)|average SoC|GPU Load|GPU Temp' | tr -s ' 	' ' ' | tr '
' '|')" >> "$1"
    sleep 2
  done
}

configura(){ # $1 = reloj (auto|high), $2 = load (mmap|none)
  echo "$1" | sudo tee "$DPM" > /dev/null
  if [ "$2" = none ]; then
    sudo mkdir -p "$(dirname "$DROP")"
    printf '[Service]\nEnvironment=LLAMA_ARG_LOAD_MODE=none\n' | sudo tee "$DROP" > /dev/null
  else
    sudo rm -f "$DROP"
  fi
  sudo systemctl daemon-reload
  sudo systemctl restart llama-flashnext
}

restaura(){
  kill $VIG 2>/dev/null
  log "restaurando configuracion BASE (auto + mmap)"
  echo auto | sudo tee "$DPM" > /dev/null
  sudo rm -f "$DROP"; sudo systemctl daemon-reload
  sudo systemctl restart llama-flashnext
  sudo systemctl start llama-guard.timer
  espera_salud && log "produccion OK" || log "AVISO: sin /health"
  log "FIN (salida en $OUT)"
}

sudo systemctl stop llama-guard.timer
vigila_memoria & VIG=$!
trap restaura EXIT
log "kernel $(uname -r); build $(readlink /models/llama-current)"

LLAMA_API_KEY=""
while IFS= read -r l; do
  l=$(echo "$l" | tr -d '[:space:]'); [ -z "$l" ] && continue; case $l in \#*) continue;; esac
  if curl -sf -o /dev/null -H "Authorization: Bearer $l" http://127.0.0.1:8080/v1/models; then LLAMA_API_KEY=$l; break; fi
done < /etc/llama-server/api-keys.txt
[ -z "$LLAMA_API_KEY" ] && { log "ERROR: ninguna clave valida"; exit 1; }
export LLAMA_API_KEY

n=0
for brazo in A B C C B A; do
  n=$((n+1))
  case $brazo in A) r=auto; m=mmap;; B) r=high; m=mmap;; C) r=high; m=none;; esac
  log "tanda $n brazo $brazo (reloj=$r, load=$m)"
  t0=$(date +%s); configura $r $m
  if ! espera_salud; then log "ERROR: el servidor no arranca en brazo $brazo"; exit 1; fi
  log "  arranque $(( $(date +%s) - t0 )) s; dpm=$(cat $DPM); MemAvailable=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo) MB; RssAnon=$(grep RssAnon /proc/$(systemctl show -p MainPID --value llama-flashnext)/status | awk '{print $2}') kB"
  muestrea_reloj "$OUT/reloj-$n-$brazo.txt" & MR=$!
  python3 "$SNAP/scripts/bench-real.py" --url http://127.0.0.1:8080 --brazo mtp --pasadas 2 \
    > "$OUT/bench-$n-$brazo.txt" 2>&1
  rc=$?; kill $MR 2>/dev/null
  f=$(ls -t "$SNAP"/benchmarks/real-mtp-*.jsonl 2>/dev/null | head -1)
  [ -n "$f" ] && mv "$f" "$OUT/real-$n-$brazo.jsonl"
  log "  tanda $n terminada (rc=$rc)"
done
