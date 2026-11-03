#!/bin/bash
# Dos clientes piden a la vez modelos grandes distintos (Hermes con DeepSeek y opencode con Qwen).
K=$(cat ${M5_KEY_FILE:?exporta M5_KEY_FILE})
pide() { local t0=$(date +%s); local c=$(curl -s -o /dev/null -w "%{http_code}" -m 900 -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":\"Responde solo: OK\"}],\"max_tokens\":600,\"chat_template_kwargs\":{\"enable_thinking\":false}}" ${M5_URL:?exporta M5_URL=http://<m5>:8080}/v1/chat/completions); echo "$(date +%T) $1 -> $c en $(( $(date +%s)-t0 )) s"; }
pide deepseek-v4.1-flash & sleep 3; pide qwen3.8-flash-next & wait
