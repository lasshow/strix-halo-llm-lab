#!/usr/bin/env bash
# Comprobacion rapida: el servidor esta vivo, carga el modelo y responde con coherencia.
#
# CONTRATO: sale 0 solo si TODAS las comprobaciones pasan. Cualquier fallo -> exit 1.
# Esto no es cosmetico: la version anterior imprimia el error y salia 0, asi que
# un servidor que respondia mal daba "verde" en la bitacora (ver H-021).
#
# Uso: LLAMA_API_KEY=... ./smoke-test.sh [url]
set -uo pipefail

URL="${1:-http://localhost:8080}"
: "${LLAMA_API_KEY:?Falta LLAMA_API_KEY}"
AUTH="Authorization: Bearer ${LLAMA_API_KEY}"
FALLOS=0

fallo() { echo "  ❌ $*"; FALLOS=$((FALLOS + 1)); }
ok()    { echo "  ✅ $*"; }

echo "== 1. Modelos cargados"
MODELS=$(curl -s --max-time 30 -H "$AUTH" "$URL/v1/models") || true
if ! echo "$MODELS" | python3 -c '
import sys, json
d = json.load(sys.stdin)["data"]
assert d, "lista vacia"
for m in d:
    print("  -", m["id"])
' 2>/dev/null; then
  fallo "no pude listar modelos (servidor caido, URL mala o auth mala)"
  echo "     respuesta: ${MODELS:0:200}"
  echo; echo "RESULTADO: FALLO ($FALLOS)"; exit 1
fi
ok "endpoint /v1/models responde"

echo
echo "== 2. La autenticacion RECHAZA lo que debe"
for etiqueta_y_hdr in "sin-auth:" "clave-falsa:Authorization: Bearer clave-invalida-de-prueba"; do
  etiqueta="${etiqueta_y_hdr%%:*}"; hdr="${etiqueta_y_hdr#*:}"
  if [[ -n "$hdr" ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -H "$hdr" "$URL/v1/models")
  else
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$URL/v1/models")
  fi
  if [[ "$code" == "401" ]]; then ok "$etiqueta -> 401"; else fallo "$etiqueta -> $code (esperado 401)"; fi
done

echo
echo "== 3. Prueba de coherencia (aritmetica + seguir instruccion)"
# Los modelos con razonamiento (Qwen3.8-Flash-Next, GLM-5.x) emiten primero
# reasoning_content y solo despues content. Con presupuesto corto se agota
# razonando y content llega vacio (H-019). Aqui eso es un FALLO, no un aprobado:
# la aplicacion cliente recibe una respuesta vacia. Se distingue del caso
# "numero equivocado" para poder diagnosticar, pero ambos cuentan como fallo.
RESP=$(curl -s --max-time 300 -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Cuanto es 17*23? Responde solo el numero."}],"max_tokens":2048,"temperature":0}' \
  "$URL/v1/chat/completions")

VEREDICTO=$(echo "$RESP" | python3 -c '
import sys, json, re
try:
    msg = json.load(sys.stdin)["choices"][0]["message"]
except Exception as e:
    print("MALFORMADA|" + str(e)[:80]); raise SystemExit
content = (msg.get("content") or "").strip()
razon   = (msg.get("reasoning_content") or "").strip()
if not content:
    estado = "VACIA_CON_RAZON" if razon else "VACIA"
    print(estado + "|razonamiento=" + str(len(razon)) + " chars"); raise SystemExit
# match EXACTO sobre los digitos del contenido: "1391" o "39" no deben pasar
digitos = re.findall(r"\d+", content)
print(("CORRECTO" if digitos == ["391"] else "INCORRECTO") + "|" + content[:120].replace("\n", " "))
')
ESTADO="${VEREDICTO%%|*}"; DETALLE="${VEREDICTO#*|}"
echo "  respuesta: $DETALLE"
case "$ESTADO" in
  CORRECTO)        ok "391 exacto en content" ;;
  INCORRECTO)      fallo "content no es 391 — revisa cuantizacion o backend (un backend roto responde, pero mal)" ;;
  VACIA_CON_RAZON) fallo "content VACIO, solo razonamiento — bucle de razonamiento (H-019): sube max_tokens o enable_thinking:false" ;;
  VACIA)           fallo "content VACIO y sin razonamiento" ;;
  MALFORMADA)      fallo "respuesta no parseable: $DETALLE" ;;
  *)               fallo "estado inesperado: $VEREDICTO" ;;
esac

echo
echo "== 4. Rendimiento reportado"
echo "$RESP" | python3 -c '
import sys, json
t = json.load(sys.stdin).get("timings", {})
if t:
    print("  prefill: %.1f t/s" % t.get("prompt_per_second", 0))
    print("  generacion: %.2f t/s" % t.get("predicted_per_second", 0))
else:
    print("  (el servidor no devolvio timings)")
' 2>/dev/null || echo "  (no pude leer timings)"

echo
if (( FALLOS == 0 )); then
  echo "RESULTADO: OK — todas las comprobaciones pasan"
  exit 0
fi
echo "RESULTADO: FALLO ($FALLOS comprobacion(es))"
exit 1
