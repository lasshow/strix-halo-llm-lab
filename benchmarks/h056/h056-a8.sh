#!/bin/bash
# Parte A8 de H-056: espera a que la ventana del M5 llegue a la bateria de 27, la pasa por tunel,
# recoge todo al repo y deja el resumen en benchmarks/h056/RESUMEN.txt
set -u
M5=${M5_SSH:?define M5_SSH}; L=~/strix-halo-llm-lab; O=$L/benchmarks/h056; mkdir -p $O
until ssh -o BatchMode=yes $M5 'grep -q "esperando bateria27\|RESTAURADO" /tmp/h056/estado 2>/dev/null'; do sleep 60; done
if ssh -o BatchMode=yes $M5 'grep -q "esperando bateria27" /tmp/h056/estado'; then
  ssh -o BatchMode=yes -f -N -L 18081:127.0.0.1:8081 $M5; sleep 3
  cd $L && LLAMA_URL=http://127.0.0.1:18081 python3 scripts/bench-calidad.py --etiqueta uncen --sin-razonamiento --max-tokens 4096 > $O/bateria.txt 2>&1
  python3 scripts/verifica-codigo.py --etiqueta uncen > $O/verif.txt 2>&1
  pkill -f "18081:127.0.0.1:8081"
fi
ssh -o BatchMode=yes $M5 'sudo -n touch /tmp/h056/bateria.ok'
until ssh -o BatchMode=yes $M5 'grep -q RESTAURADO /tmp/h056/estado'; do sleep 30; done
sleep 120
scp -q "$M5:/tmp/h056/*" $O/ 2>/dev/null
sed -i -E 's#192\.168\.[0-9.]+#<M5>#g; s#/home/lasso#~#g' $O/*.txt 2>/dev/null
P=$(ssh -o BatchMode=yes $M5 'systemctl is-active llama-flashnext')
H=$(curl -s -m 20 -o /dev/null -w %{http_code} -H "Authorization: Bearer $(cat ${M5_KEY_FILE:?})" http://${M5_HOST:?}:8080/v1/models)
{
echo "PRODUCCION tras la prueba: servicio=$P  /v1/models=$H"
echo; echo "--- negativas produccion"; tail -1 $O/negativas-prod.txt
echo "--- negativas uncensored"; tail -1 $O/negativas-uncen.txt
echo; echo "--- velocidad"; grep "pasada 2" $O/velocidad.txt
echo; echo "--- calidad 12"; tail -1 $O/calidad12.txt
echo; echo "--- aptitud"; tail -12 $O/aptitud.txt
echo; echo "--- celdas"; tail -5 $O/celdas.txt
echo; echo "--- contexto"; grep "^|" $O/contexto.txt
echo; echo "--- bateria 27"; tail -4 $O/bateria.txt
echo; echo "--- codigo"; grep -E "^\s*(PASA|COMPILA|NO COMPILA)" $O/verif.txt
} > $O/RESUMEN.txt 2>&1
cat $O/RESUMEN.txt
