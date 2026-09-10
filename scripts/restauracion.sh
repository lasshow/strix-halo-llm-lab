#!/usr/bin/env bash
# Comprobacion de restauracion tras un barrido: ¿quedo produccion como estaba?
#
# Se escribe como script versionado a proposito. Durante los pilotos del
# 10-09-2026 esta comprobacion se tecleaba a mano cada vez, y asi se colo un
# smoke que exigia la cadena literal "OK" con max_tokens=8: el modelo respondio
# "Tudo bem!..." y parecio un fallo del servicio cuando el servicio estaba
# sano. Una comprobacion que da falsos rojos acaba ignorandose, que es peor que
# no tenerla.
#
# Uso:  restauracion.sh [unidad] [puerto] [modelo_esperado]
# Sale 0 si todo pasa, 1 a la primera comprobacion que falle.
set -uo pipefail

UNIDAD="${1:-llama-flashnext}"
PUERTO="${2:-8080}"
MODELO="${3:-qwen3.8-flash-next}"
BENCH="${UNIDAD}-bench"
FALLOS=0

ok()    { echo "  [OK] $*"; }
fallo() { echo "  [X]  $*"; FALLOS=$((FALLOS+1)); }

echo "== Restauracion de ${UNIDAD} =="

# 1. unidad activa y habilitada
[ "$(systemctl is-active "$UNIDAD")" = active ] \
  && ok "unidad activa" || fallo "unidad NO activa"
[ "$(systemctl is-enabled "$UNIDAD" 2>/dev/null)" = enabled ] \
  && ok "unidad habilitada (arranca sola tras reinicio)" \
  || fallo "unidad NO habilitada"

# 2. sin residuos del banco
if systemctl list-unit-files 2>/dev/null | grep -q "^${BENCH}.service"; then
  fallo "queda la unidad de banco ${BENCH}"
else
  ok "unidad de banco eliminada"
fi
[ -f "/etc/systemd/system/${BENCH}.service" ] \
  && fallo "queda el fichero de unidad de banco" \
  || ok "sin fichero de unidad de banco"

# 3. endpoint sano
COD=$(curl -s -m 15 -o /dev/null -w "%{http_code}" "localhost:${PUERTO}/health")
[ "$COD" = 200 ] && ok "/health responde 200" || fallo "/health devuelve ${COD}"

# 4. clave y modelo
K=$(sudo -n sed -n "s/^LLAMA_API_KEY=//p" /etc/llama-server/*.env 2>/dev/null | head -1)
if [ -z "$K" ]; then
  fallo "no pude leer la clave de API"
else
  ACTUAL=$(curl -s -m 15 -H "Authorization: Bearer $K" \
    "localhost:${PUERTO}/v1/models" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)
  [ "$ACTUAL" = "$MODELO" ] \
    && ok "modelo servido: ${ACTUAL}" \
    || fallo "modelo servido ${ACTUAL:-<ninguno>}, esperaba ${MODELO}"

  # 5. smoke: se exige RESPUESTA NO VACIA con finalizacion normal, no una
  #    cadena concreta. Comprobar la corrección del contenido es trabajo de
  #    smoke-test.sh, que da presupuesto de tokens suficiente; aqui solo se
  #    verifica que el servicio atiende de verdad.
  R=$(curl -s -m 90 -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
    -d "{\"model\":\"${MODELO}\",\"messages\":[{\"role\":\"user\",\"content\":\"Responde con una palabra.\"}],\"max_tokens\":64,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
    "localhost:${PUERTO}/v1/chat/completions")
  VEREDICTO=$(printf '%s' "$R" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("NO_JSON"); raise SystemExit
c = d.get("choices") or []
if not c:
    print("SIN_CHOICES"); raise SystemExit
msg = c[0].get("message") or {}
cont = (msg.get("content") or "").strip()
fin = c[0].get("finish_reason")
if not cont:
    print("VACIA")
elif fin != "stop":
    print("TRUNCADA|" + str(fin))
else:
    print("RESPONDE|" + cont[:40].replace("\n", " "))
' 2>/dev/null)
  case "${VEREDICTO%%|*}" in
    RESPONDE) ok "smoke autenticado: ${VEREDICTO#*|}" ;;
    VACIA)    fallo "smoke: respuesta VACIA" ;;
    TRUNCADA) fallo "smoke: finalizacion anomala (${VEREDICTO#*|})" ;;
    *)        fallo "smoke: sin respuesta util (${VEREDICTO:-vacio})" ;;
  esac

  # 6. control negativo: sin clave debe rechazar
  SIN=$(curl -s -m 15 -o /dev/null -w "%{http_code}" -H "Content-Type: application/json" \
    -d '{"model":"x","messages":[]}' "localhost:${PUERTO}/v1/chat/completions")
  [ "$SIN" = 401 ] \
    && ok "sin clave devuelve 401 (la autenticacion sigue puesta)" \
    || fallo "sin clave devuelve ${SIN}, esperaba 401"
fi

echo
if [ "$FALLOS" -eq 0 ]; then
  echo "RESULTADO: OK - produccion restaurada"
  exit 0
fi
echo "RESULTADO: ${FALLOS} comprobacion(es) fallidas - REVISAR"
exit 1
