#!/usr/bin/env bash
# H-036: los cambios de drluoto, UNO por campana, contra la produccion vigente.
#
#   cadena-h036.sh <brazo> [run-id]     brazo = nmax3 | frspec | requant | kvzero
#
# Cada brazo es una campana de campana.py con UNA fase: para produccion, mide
# A/B (np=2, 2 concurrentes, greedy por logprobs contra referencia sin
# especulacion), y si adopta escribe la unidad (aplicar) y/o promueve la build
# candidata, pasa el gate y revierte si sale rojo. Los brazos son acumulativos:
# el control de cada uno es lo que dejo desplegado el anterior.
#
# Builds:
#   baseline  = la que corre (readlink /models/llama-current), sin construir.
#   candidato = solo la necesitan frspec (parches FR-Spec) y kvzero (parche KV):
#               se pasa --patch y builds.sh la construye si no existe.
set -euo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAZO="${1:?uso: cadena-h036.sh <nmax3|frspec|requant|kvzero> [run-id]}"
RUN_ID="${2:-h036-${BRAZO}-$(date +%Y%m%d-%H%M)}"
SALIDA="${SALIDA:-$HOME/campanas/${RUN_ID}}"
BASE="df03399b885831b2a1603b3abb0d8c156808e363"
LLAMA_CURRENT="${LLAMA_CURRENT:-/models/llama-current}"

# la baseline es exactamente la build productiva: id = basename del symlink
ACTUAL_ID="$(basename "$(readlink -f "$LLAMA_CURRENT")")"
case "$ACTUAL_ID" in
  "$BASE"+*) ;;
  *) echo "la build productiva ($ACTUAL_ID) no es $BASE+<parche>: H-036 asume df03399" >&2; exit 2 ;;
esac
# el parche congelado de la build productiva, localizado en patches/ por su sha256[:8]
SHA8="$(python3 -c "import json,sys,os; m=json.load(open(os.path.join(os.path.realpath(sys.argv[1]),'manifest.json'))); print((m.get('patch_sha256') or '')[:8])" "$LLAMA_CURRENT")"
PARCHE_BASE=""
for p in "${AQUI}"/../patches/*.patch; do
  [ "$(sha256sum "$p" | cut -c1-8)" = "$SHA8" ] && PARCHE_BASE="$p"
done
[ -n "$PARCHE_BASE" ] || { echo "no encuentro en patches/ el parche ${SHA8} de la build productiva" >&2; exit 2; }

case "$BRAZO" in
  nmax3|requant)
    # misma build en los dos brazos
    exec python3 "${AQUI}/campana.py" --run-id "$RUN_ID" \
      --baseline-sha "$BASE" --patch-baseline "$PARCHE_BASE" \
      --candidato-sha "$BASE" --patch "$PARCHE_BASE" \
      --saltar-construccion --salida "$SALIDA" \
      --fase "fases_h036.py:${BRAZO}" ;;
  frspec)
    PARCHE_CAND="${AQUI}/../patches/mtp-stack-frspec.patch"
    [ -f "$PARCHE_CAND" ] || { echo "falta $PARCHE_CAND (parches FR-Spec portados)" >&2; exit 2; }
    exec python3 "${AQUI}/campana.py" --run-id "$RUN_ID" \
      --baseline-sha "$BASE" --patch-baseline "$PARCHE_BASE" \
      --candidato-sha "$BASE" --patch "$PARCHE_CAND" \
      --salida "$SALIDA" --fase fases_h036.py:frspec ;;
  kvzero)
    PARCHE_CAND="${AQUI}/../patches/mtp-stack-kvzero.patch"
    [ -f "$PARCHE_CAND" ] || { echo "falta $PARCHE_CAND" >&2; exit 2; }
    exec python3 "${AQUI}/campana.py" --run-id "$RUN_ID" \
      --baseline-sha "$BASE" --patch-baseline "$PARCHE_BASE" \
      --candidato-sha "$BASE" --patch "$PARCHE_CAND" \
      --salida "$SALIDA" --fase fases_h036.py:kvzero ;;
  *) echo "brazo desconocido: $BRAZO" >&2; exit 2 ;;
esac
