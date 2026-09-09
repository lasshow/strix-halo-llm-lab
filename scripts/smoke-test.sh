#!/usr/bin/env bash
# Comprobacion rapida: el servidor esta vivo, carga el modelo y responde con coherencia.
# Uso: LLAMA_API_KEY=... ./smoke-test.sh [url]
set -euo pipefail

URL="${1:-http://localhost:8080}"
: "${LLAMA_API_KEY:?Falta LLAMA_API_KEY}"
AUTH="Authorization: Bearer ${LLAMA_API_KEY}"

echo "== Modelos cargados"
curl -sf -H "$AUTH" "$URL/v1/models" | python3 -c '
import sys, json
for m in json.load(sys.stdin)["data"]:
    print("  -", m["id"])
'

echo
echo "== Prueba de coherencia (aritmetica + instruccion)"
# Nota: los modelos con razonamiento (Qwen3.8-Flash-Next, GLM-5.x) emiten primero
# reasoning_content y solo despues content. Con max_tokens bajo se agota el
# presupuesto razonando y content llega vacio -> falso negativo. De ahi el margen amplio.
RESP=$(curl -sf -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Cuanto es 17*23? Responde solo el numero."}],"max_tokens":512,"temperature":0}' \
  "$URL/v1/chat/completions")

OUT=$(echo "$RESP" | python3 -c '
import sys, json
m = json.load(sys.stdin)["choices"][0]["message"]
# buscar en content y, si viene vacio, en el razonamiento
txt = (m.get("content") or "").strip()
if not txt:
    txt = (m.get("reasoning_content") or "").strip()
print(txt)
')
echo "  respuesta: ${OUT:0:120}"
if [[ "$OUT" == *"391"* ]]; then
  echo "  ✅ correcto"
else
  echo "  ❌ ESPERADO 391 — revisa cuantizacion o backend (un backend roto responde, pero mal)"
fi

echo
echo "== Rendimiento reportado"
echo "$RESP" | python3 -c '
import sys, json
t = json.load(sys.stdin).get("timings", {})
if t:
    pp = t.get("prompt_per_second", 0)
    tg = t.get("predicted_per_second", 0)
    print("  prefill: %.1f t/s" % pp)
    print("  generacion: %.2f t/s" % tg)
else:
    print("  (el servidor no devolvio timings)")
'
