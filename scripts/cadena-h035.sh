#!/usr/bin/env bash
# H-035: cabeza MTP sidecar en la linea productiva (np=2 -kvu), con despliegue.
#
# Corre las cuatro fases de fases_h035.py con campana.py, que:
#   - para produccion (ventana autorizada), lanza los bancos en :8081,
#   - si TODAS las fases adoptan: escribe los flags MTP en la unidad
#     (banco.pon_mtp via el `aplicar` de la fase 3), promueve la build
#     candidata (builds.sh promover) y reinicia;
#   - pasa el gate (restauracion.sh + smoke-test.sh); rojo => revierte unidad
#     Y build y vuelve a comprobar;
#   - si alguna fase no adopta: `aplicar` deja la unidad sin MTP (= la de hoy)
#     y no promueve nada; produccion se rearranca tal cual.
#
# Baseline = lo que corre hoy: df03399 + PR28501 (patches/pr28501-vulkan.patch).
# Candidato = df03399 + PR28501 + PR28243 (patches/mtp-stack-pr28501-pr28243.patch).
# Ambas builds ya estan en /models/llama-builds (--saltar-construccion).
#
# Uso (en el M5, desde ~/lab):
#   nohup setsid bash scripts/cadena-h035.sh <run-id> > ~/campanas/<run-id>.log 2>&1 < /dev/null &
set -euo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID="${1:-h035-$(date +%Y%m%d)}"
BASE="df03399b885831b2a1603b3abb0d8c156808e363"
PARCHE_BASE="${AQUI}/../patches/pr28501-vulkan.patch"
PARCHE_CAND="${AQUI}/../patches/mtp-stack-pr28501-pr28243.patch"
SALIDA="${SALIDA:-$HOME/campanas/${RUN_ID}}"

for p in "$PARCHE_BASE" "$PARCHE_CAND"; do
  [ -f "$p" ] || { echo "falta el parche $p" >&2; exit 2; }
done

exec python3 "${AQUI}/campana.py" \
  --run-id "$RUN_ID" \
  --baseline-sha "$BASE" --patch-baseline "$PARCHE_BASE" \
  --candidato-sha "$BASE" --patch "$PARCHE_CAND" \
  --saltar-construccion \
  --salida "$SALIDA" \
  --fase fases_h035.py:diagnostico_greedy \
  --fase fases_h035.py:np2_kvu_aislamiento \
  --fase fases_h035.py:np2_kvu_velocidad \
  --fase fases_h035.py:np2_kvu_vision
