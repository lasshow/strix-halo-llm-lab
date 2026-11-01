#!/bin/bash
# H-056 ventana: Qwen3.8-Flash-Next Uncensored (vmlinux Gufo-Q4Mix) en :8081, produccion parada.
# Restaura produccion SIEMPRE al salir (trap). Lanzar con nohup setsid como root.
set -u
D=/tmp/h056; mkdir -p $D; cd /tmp/lab/scripts
M=/models/gguf/qwen38-flash-next
U=$M/uncensored-vmlinux/Qwen3.8-Flash-Next-Uncensored-Gufo-Q4Mix.gguf
K=$(grep -v "^#" /etc/llama-server/api-keys.txt | head -1 | tr -d "[:space:]")
export MOTOR_KEY=$K LLAMA_API_KEY=$K
IMG=$(systemctl show llama-flashnext -p Environment --value | grep -o "GUFO_IMG=[^ ]*" | tail -1 | cut -d= -f2)
B=http://127.0.0.1:8081/v1; MD=qwen3.8-flash-next

restaura() {
  runuser -u lasso -- env XDG_RUNTIME_DIR=/run/user/1000 podman rm -f gufo-uncen >/dev/null 2>&1
  rm -f /etc/llama-guard.disable
  systemctl start llama-flashnext
  echo "RESTAURADO $(date +%T) $(systemctl is-active llama-flashnext)" | tee -a $D/estado
}
trap restaura EXIT

echo "== negativas PRODUCCION $(date +%T)" > $D/estado
python3 /tmp/h056-negativas.py http://127.0.0.1:8080/v1 > $D/negativas-prod.txt 2>&1

touch /etc/llama-guard.disable
systemctl stop llama-flashnext; sleep 10
runuser -u lasso -- env XDG_RUNTIME_DIR=/run/user/1000 podman run -d --rm --name gufo-uncen \
  --device /dev/kfd --device /dev/dri --group-add keep-groups --ulimit memlock=-1 --security-opt label=disable \
  -v /models:/models:ro -p 127.0.0.1:8081:8081 "$IMG" \
  gufo serve --host 0.0.0.0 --port 8081 --sessions 2 --api-key "$K" llm \
    -m $U --mmproj $M/uncensored-vmlinux/mmproj-BF16.gguf \
    --mtp-model $M/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf --speculative mtp \
    --context 65536 --cache-ram-bytes 4294967296 --served-model-name $MD >> $D/estado 2>&1
for i in $(seq 1 180); do
  [ "$(curl -s -o /dev/null -w %{http_code} -H "Authorization: Bearer $K" 127.0.0.1:8081/health)" = 200 ] && break; sleep 5
done
echo "health $(curl -s -o /dev/null -w %{http_code} -H "Authorization: Bearer $K" 127.0.0.1:8081/health) $(date +%T)" >> $D/estado
runuser -u lasso -- env XDG_RUNTIME_DIR=/run/user/1000 podman logs gufo-uncen 2>&1 | tail -20 > $D/arranque.txt

echo "== negativas UNCEN" >> $D/estado
python3 /tmp/h056-negativas.py $B > $D/negativas-uncen.txt 2>&1
echo "== velocidad $(date +%T)" >> $D/estado
sed "s#127.0.0.1:8080#127.0.0.1:8081#" /tmp/corpus-h049/real-usage.py > /tmp/real-8081.py
python3 /tmp/real-8081.py /tmp/corpus-h049 $D/real.jsonl > $D/velocidad.txt 2>&1
echo "== calidad12 $(date +%T)" >> $D/estado
python3 /tmp/corpus-h049/bench-calidad-HEAD.py --base $B --model $MD --etiqueta uncen --salida $D/cal.jsonl --bust-cache > $D/calidad12.txt 2>&1
echo "== aptitud $(date +%T)" >> $D/estado
python3 h049-pruebas.py --base $B --model $MD --etiqueta uncen --salida $D/aptitud.json > $D/aptitud.txt 2>&1
echo "== celdas $(date +%T)" >> $D/estado
python3 h045-celdas-dudosas.py --base $B --model $MD --etiqueta uncen --pasadas 3 --salida $D/celdas.jsonl > $D/celdas.txt 2>&1
echo "== contexto $(date +%T)" >> $D/estado
python3 bench-context.py --url http://127.0.0.1:8081 --model $MD --tokens 2000 16000 32000 60000 --passes 2 --needle --jsonl $D/ctx.jsonl > $D/contexto.txt 2>&1
echo "== esperando bateria27 del A8 (fichero /tmp/h056/bateria.ok) $(date +%T)" >> $D/estado
for i in $(seq 1 120); do [ -f $D/bateria.ok ] && break; sleep 15; done
echo "== FIN $(date +%T)" >> $D/estado
