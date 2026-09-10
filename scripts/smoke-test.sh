#!/usr/bin/env bash
# Comprobacion rapida: el servidor esta vivo, carga el modelo y responde con coherencia.
#
# CONTRATO: sale 0 solo si TODAS las comprobaciones pasan. Cualquier fallo -> exit 1.
# Esto no es cosmetico: la version anterior imprimia el error y salia 0, asi que
# un servidor que respondia mal daba "verde" en la bitacora (ver H-021).
#
# Correcciones H-024 (auditoria externa del corte 35a2f26):
#   - El veredicto usaba re.findall(r"\d+", content) == ["391"], que ACEPTA
#     "-391", "391%", "391 unidades" y "No es 391". Solo rechazaba "1391".
#     Ahora se compara el contenido normalizado COMPLETO con "391".
#   - No se miraba finish_reason: una respuesta cortada por presupuesto
#     ("39" de "391") podia pasar. Ahora finalizacion anormal es fallo.
#   - Los timings ausentes se imprimian como aviso y el test seguia en verde.
#     Una medida ausente no es una medida: ahora es fallo.
#   - La cabecera de autenticacion mandaba la cadena literal "Bearer ***" en
#     lugar de la clave, asi que el test no podia pasar nunca contra un
#     servidor con autenticacion. Ahora usa $LLAMA_API_KEY y solo se
#     ENSENA enmascarada por pantalla.
#
# Uso: LLAMA_API_KEY=... ./smoke-test.sh [url]
set -uo pipefail

URL="${1:-${LLAMA_URL:-http://localhost:8080}}"
: "${LLAMA_API_KEY:?Falta LLAMA_API_KEY}"
# La clave VA en la cabecera; nunca se imprime (abajo solo se muestra
# enmascarada). Antes esto contenia el literal '***', que se enviaba tal cual
# y hacia fallar la autenticacion contra cualquier servidor con clave.
AUTH="Authorization: Bearer ${LLAMA_API_KEY}"
echo "== servidor: $URL  (clave: ${LLAMA_API_KEY:0:3}...${LLAMA_API_KEY: -2})"
FALLOS=0

fallo() { echo "  [X] $*"; FALLOS=$((FALLOS + 1)); }
ok()    { echo "  [OK] $*"; }

echo
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
for etiqueta_y_hdr in "sin-auth:" "clave-falsa:Authorization: Bearer clave-de-prueba-invalida"; do
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
# Contrato de esta prueba (tarea con resultado comprobable):
#   content normalizado == "391" Y finalizacion normal.
# Los modelos con razonamiento (Qwen3.8-Flash-Next, GLM-5.x) emiten primero
# reasoning_content y solo despues content. Con presupuesto corto se agota
# razonando y content llega vacio (H-019): eso es FALLO, no aprobado.
RESP=$(curl -s --max-time 300 -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Cuanto es 17*23? Responde solo el numero."}],"max_tokens":2048,"temperature":0}' \
  "$URL/v1/chat/completions")

VEREDICTO=$(echo "$RESP" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    ch = d["choices"][0]
    msg = ch["message"]
except Exception as e:
    print("MALFORMADA|" + str(e)[:80]); raise SystemExit
content = (msg.get("content") or "").strip()
razon   = (msg.get("reasoning_content") or "").strip()
fin     = ch.get("finish_reason") or ""
if not content:
    estado = "VACIA_CON_RAZON" if razon else "VACIA"
    print(estado + "|razonamiento=" + str(len(razon)) + " chars"); raise SystemExit
if fin and fin != "stop":
    print("TRUNCADA|finish_reason=" + fin + " content=" + content[:80].replace("\n", " "))
    raise SystemExit
# Contrato ESTRICTO: el contenido entero, normalizado, tiene que ser 391.
# Se permite solo colapsar espacios y un punto final. Deliberadamente NO se
# extraen digitos del texto: "-391", "391%", "391 unidades" y "No es 391"
# tienen que fallar, y con findall(r"\\d+") pasaban.
norm = " ".join(content.split()).rstrip(".")
print(("CORRECTO" if norm == "391" else "INCORRECTO") + "|" + content[:120].replace("\n", " "))
')
ESTADO="${VEREDICTO%%|*}"; DETALLE="${VEREDICTO#*|}"
echo "  respuesta: $DETALLE"
case "$ESTADO" in
  CORRECTO)        ok "content es exactamente 391 con finalizacion normal" ;;
  INCORRECTO)      fallo "content no es exactamente 391 - revisa cuantizacion o backend (un backend roto responde, pero mal)" ;;
  TRUNCADA)        fallo "respuesta cortada antes de terminar: $DETALLE" ;;
  VACIA_CON_RAZON) fallo "content VACIO, solo razonamiento - bucle de razonamiento (H-019): sube max_tokens o enable_thinking:false" ;;
  VACIA)           fallo "content VACIO y sin razonamiento" ;;
  MALFORMADA)      fallo "respuesta no parseable: $DETALLE" ;;
  *)               fallo "estado inesperado: $VEREDICTO" ;;
esac

echo
echo "== 4. Rendimiento reportado"
# Los timings ausentes son un fallo del servidor, no un detalle cosmetico:
# sin ellos no hay medida (y antes esto salia en verde).
if ! echo "$RESP" | python3 -c '
import sys, json
d = json.load(sys.stdin)
t = d.get("timings") or (d.get("usage") or {}).get("timings") or {}
faltan = [k for k in ("prompt_per_second", "predicted_per_second") if not isinstance(t.get(k), (int, float))]
if faltan:
    print("  el servidor no devolvio timings utiles, faltan:", faltan); raise SystemExit(1)
print("  prefill: %.1f t/s" % t["prompt_per_second"])
print("  generacion: %.2f t/s" % t["predicted_per_second"])
'; then
  fallo "sin metricas de rendimiento en la respuesta"
fi

echo
if (( FALLOS == 0 )); then
  echo "RESULTADO: OK - todas las comprobaciones pasan"
  exit 0
fi
echo "RESULTADO: FALLO ($FALLOS comprobacion(es))"
exit 1
