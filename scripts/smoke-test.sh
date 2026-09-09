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
RESP=$(curl -sf -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Cuanto es 17*23? Responde solo el numero."}],"max_tokens":16,"temperature":0}' \
  "$URL/v1/chat/completions")

OUT=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip())')
echo "  respuesta: $OUT"
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
    print(f"  prefill: {t.get(\"prompt_per_second\", 0):.1f} t/s")
    print(f"  generacion: {t.get(\"predicted_per_second\", 0):.2f} t/s")
else:
    print("  (el servidor no devolvio timings)")
'
