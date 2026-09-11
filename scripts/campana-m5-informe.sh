#!/bin/bash
# Entrega el informe de la campana nocturna del M5 cuando exista; si no, vacio (no envia nada).
# Si la campana lleva >10 h sin terminar o el proceso murio sin informe, avisa.
H=${M5_HOST:?define M5_HOST}
EST=$(ssh -o BatchMode=yes -o ConnectTimeout=15 $H 'test -f ~/campana/INFORME.md && echo INFORME; pgrep -af campana-nocturna | grep -v pgrep | wc -l; tail -3 ~/campana/campana.log 2>/dev/null; systemctl is-active llama-flashnext' 2>/dev/null) || { echo "M5 inalcanzable por tailnet al comprobar la campana nocturna."; exit 0; }
STATE=/home/lasso/.hermes/scripts/state/campana-m5.entregado
if grep -q '^INFORME$' <<<"$EST"; then
  [ -f "$STATE" ] && exit 0
  ssh -o BatchMode=yes $H 'cat ~/campana/INFORME.md'
  touch "$STATE"; exit 0
fi
VIVOS=$(sed -n 1p <<<"$EST"); grep -q INFORME <<<"$EST" || VIVOS=$(sed -n 1p <<<"$EST")
if [ "$VIVOS" = "0" ]; then
  echo "Campana M5: el proceso ha MUERTO sin generar informe. Ultimas lineas:"; sed -n '2,4p' <<<"$EST"; echo "Produccion: $(tail -1 <<<"$EST")"
  touch "$STATE"
fi
