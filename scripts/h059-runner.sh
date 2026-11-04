#!/bin/bash
# H-059: repetir las rondas de H-054/H-055 tras Gufo 0.29.0 + kernel 7.2.15 (04-nov-2026)
set -u
LAB=~/strix-halo-llm-lab
M5="ssh -o BatchMode=yes ${M5_SSH:?exporta M5_SSH=usuario@host}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
R=campanas/h059-$TS          # en el M5
L=~/.hermes/cache/scratch/h059-$TS; mkdir -p $L
log(){ echo "[$(date +%T)] $*" | tee -a $L/runner.log; }

log "preparando $R en el M5"
$M5 "mkdir -p ~/$R/corpus ~/$R/s" || exit 1
git -C $LAB archive a91ff6a | $M5 "tar x -C ~/$R/corpus" || exit 1
scp -q $LAB/scripts/{h049-pruebas,h045-celdas-dudosas,bench-context,bench-calidad-motores,validacion}.py \
       $LAB/evidencias/h054-gufo-0281-20261101/real-usage.py $M5_SSH:$R/s/ || exit 1
$M5 "sudo -n cat /etc/llama-server/api-keys.txt | grep -v '^#' | grep . | head -1 > ~/$R/.k; chmod 600 ~/$R/.k" || exit 1
$M5 "uname -r; systemctl show llama-flashnext -p Environment --value | grep -o 'GUFO_IMG=[^ ]*'" > $L/entorno.txt
B=http://127.0.0.1:8080/v1
E="cd ~/$R && export MOTOR_KEY=\$(cat .k) LLAMA_API_KEY=\$(cat .k)"

log "1/6 carga real (6 tareas, corpus a91ff6a)"
$M5 "cd ~/$R && sudo -n python3 s/real-usage.py corpus real.jsonl" > $L/real.txt 2>&1; log "  rc=$?"
log "2/6 aptitud (fugas, concurrencia, vision, tools)"
$M5 "$E && python3 s/h049-pruebas.py --base $B --model qwen3.8-flash-next --etiqueta gufo-0.29.0 --salida aptitud.json" > $L/aptitud.txt 2>&1; log "  rc=$?"
log "3/6 celdas dudosas x3"
$M5 "$E && python3 s/h045-celdas-dudosas.py --base $B --model qwen3.8-flash-next --etiqueta gufo-0.29.0 --salida celdas.jsonl" > $L/celdas.txt 2>&1; log "  rc=$?"
log "4/6 contexto con aguja"
$M5 "$E && python3 s/bench-context.py --url http://127.0.0.1:8080 --model qwen3.8-flash-next --tokens 2000 16000 32000 60000 --passes 2 --needle --jsonl contexto.jsonl" > $L/contexto.txt 2>&1; log "  rc=$?"
log "5/6 calidad 12 celdas --bust-cache"
$M5 "$E && python3 s/bench-calidad-motores.py --base $B --model qwen3.8-flash-next --etiqueta gufo-0.29.0 --salida cal.jsonl --bust-cache" > $L/cal.txt 2>&1; log "  rc=$?"
log "6/6 bateria 27 prompts + codigo compilado (desde el A8)"
cd $LAB && LLAMA_URL=http://$M5_HOST:8080 python3 scripts/bench-calidad.py --etiqueta gufo-0290 --sin-razonamiento > $L/bateria.txt 2>&1; log "  rc=$?"
python3 scripts/verifica-codigo.py --etiqueta gufo-0290 > $L/verif.txt 2>&1; log "  rc=$?"
scp -q $M5_SSH:"$R/*.json*" $L/ ; $M5 "rm -f ~/$R/.k"
log "FIN $L"
