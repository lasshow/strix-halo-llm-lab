#!/bin/bash
# Lanza el brazo sin especulacion del banco de carga real (H-038).
# La URL del M5 se lee de fichero para no teclear IPs en la linea de comandos.
set -euo pipefail
cd "$HOME/strix-halo-llm-lab"
export LLAMA_API_KEY="$(cat "$HOME/.secrets/m5-llama-api.key")"
URL="$(cat "$HOME/.secrets/m5-llama-url.txt")"
exec python3 scripts/bench-real.py --url "$URL" --brazo sin-mtp --pasadas 2
