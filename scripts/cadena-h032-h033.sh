#!/usr/bin/env bash
# Cadena H-032 -> H-033 en el M5, una ventana. Se lanza con nohup/setsid.
# H-033 solo arranca si H-032 termino con produccion sana (salida 0 o 2).
set -uo pipefail
cd "$HOME/lab" || exit 1
BASE=df03399b885831b2a1603b3abb0d8c156808e363
PARCHE="$HOME/lab/evidencias/parches/pr28501-vulkan-266464c8.patch"
SALIDA="$HOME/campanas"
mkdir -p "$SALIDA"
FECHA=$(date +%Y%m%d)

echo "== H-032 $(date +%T)"
python3 scripts/campana.py --run-id "h032-${FECHA}" \
  --unidad llama-flashnext --modelo qwen3.8-flash-next \
  --baseline-sha "$BASE" --saltar-construccion \
  --puerto-banco 8081 --salida "$SALIDA/h032-${FECHA}" \
  --fase scripts/fases_h032.py:correccion_cache_ram \
  --fase scripts/fases_h032.py:regresion_28495 \
  --fase scripts/fases_h032.py:rendimiento_cache_ram
R32=$?
echo "== H-032 salida $R32 $(date +%T)"

if [ "$R32" -eq 0 ] || [ "$R32" -eq 2 ]; then
  echo "== H-033 $(date +%T)"
  python3 scripts/campana.py --run-id "h033-${FECHA}" \
    --unidad llama-flashnext --modelo qwen3.8-flash-next \
    --baseline-sha "$BASE" --candidato-sha "$BASE" --patch "$PARCHE" \
    --saltar-construccion \
    --puerto-banco 8081 --salida "$SALIDA/h033-${FECHA}" \
    --fase scripts/fases_h033.py:ab_builds
  R33=$?
  echo "== H-033 salida $R33 $(date +%T)"
else
  echo "== H-033 NO lanzada: H-032 dejo produccion en estado dudoso (salida $R32)"
  R33=-1
fi
echo "FIN R32=$R32 R33=$R33" > "$SALIDA/FIN-${FECHA}"
