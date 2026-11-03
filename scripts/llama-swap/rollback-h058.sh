#!/bin/bash
# Vuelta al estado anterior a H-058 (llama-swap pequeno en :9292, Gufo/Qwen en :8080 con clave).
set -u
B=.bak-h058
systemctl stop llama-swap
for u in llama-flashnext llama-uncensored llama-ds41; do systemctl stop $u; done
rm -f /etc/systemd/system/llama-flashnext.service.d/70-tras-llama-swap.conf
cp -a /usr/local/bin/m5-model$B /usr/local/bin/m5-model
cp -a /etc/systemd/system/llama-swap.service$B /etc/systemd/system/llama-swap.service
cp -a /etc/systemd/system/llama-uncensored.service$B /etc/systemd/system/llama-uncensored.service
cp -a /etc/systemd/system/llama-ds41.service$B /etc/systemd/system/llama-ds41.service
cp -a /models/llama-swap/config.yaml$B /models/llama-swap/config.yaml
cp -a /models/llama-swap/llama-swap.v268 /models/llama-swap/llama-swap
systemctl daemon-reload
rm -f /etc/llama-guard.disable
systemctl start llama-swap
/usr/local/bin/m5-model qwen
/usr/local/bin/m5-model status
