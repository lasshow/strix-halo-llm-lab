#!/usr/bin/env python3
"""H-058: E2E de la puerta unica llama-swap del M5 vista desde otro equipo.
Uso: e2e_swap.py <base> <clave_qwen_fichero> <clave_ds_fichero> <ssh_destino>"""
import json, subprocess, sys, time, urllib.request, urllib.error

base, kq, kd, ssh = sys.argv[1:5]
KQ, KD = open(kq).read().strip(), open(kd).read().strip()
res = []

def req(path, body=None, key=KQ, timeout=900, method=None):
    h = {'Content-Type': 'application/json'}
    if key:
        h['Authorization'] = 'Bearer ' + key
    r = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, h, method=method)
    t = time.time()
    try:
        raw = urllib.request.urlopen(r, timeout=timeout).read().decode()
        try:
            return 200, json.loads(raw), time.time() - t
        except Exception:
            return 200, raw, time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300], time.time() - t

def chat(model, key=KQ, txt='Responde solo: OK', max_tokens=600, extra=None):
    b = {'model': model, 'messages': [{'role': 'user', 'content': txt}], 'max_tokens': max_tokens,
         'chat_template_kwargs': {'enable_thinking': False}}
    b.update(extra or {})
    c, r, dt = req('/v1/chat/completions', b, key)
    if c != 200:
        return c, str(r)[:120], dt, None
    ch = r['choices'][0]
    return c, (ch['message'].get('content') or '').strip(), dt, r.get('usage', {})

def running():
    c, r, _ = req('/running')
    return {x['model']: x['state'] for x in r.get('running', [])} if c == 200 else f'HTTP {c}'

def m5(cmd):
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', ssh, cmd], capture_output=True, text=True, timeout=300).stdout.strip()

def caso(n, ok, det):
    res.append((n, bool(ok))); print(f"[{'OK ' if ok else 'NO '}] {n:34s} {det}", flush=True)

# 1. clave
c1 = req('/v1/chat/completions', {'model': 'qwen3.8-flash-next', 'messages': [{'role': 'user', 'content': 'hola'}], 'max_tokens': 4}, key=None)[0]
c2 = req('/v1/chat/completions', {'model': 'qwen3.8-flash-next', 'messages': [{'role': 'user', 'content': 'hola'}], 'max_tokens': 4}, key='falsa')[0]
c3 = req('/v1/models', key=None)[0]
caso('rechaza-sin-clave', c1 == 401 and c2 == 401, f'chat sin={c1} falsa={c2} | /v1/models sin clave={c3}')
c, r, _ = req('/v1/models')
ids = [m['id'] for m in r['data']] if c == 200 else r
caso('lista-modelos', c == 200 and {'qwen3.8-flash-next', 'deepseek-v4.1-flash'} <= set(ids), ids)
# 2. qwen con la clave de siempre y con la de DeepSeek (las dos valen en la puerta)
c, t, dt, u = chat('qwen3.8-flash-next')
caso('qwen-clave-qwen', c == 200 and t, f'{t!r} {dt:.1f}s')
c, t, dt, u = chat('qwen3.8-flash-next', key=KD)
caso('qwen-clave-ds', c == 200 and t, f'{t!r} {dt:.1f}s')
# 3. sobrecoste del proxy en generacion: misma peticion por la puerta y directa al motor
cuerpo = {'model': 'qwen3.8-flash-next', 'messages': [{'role': 'user', 'content': 'Escribe un cuento de 300 palabras sobre un horno industrial.'}],
          'max_tokens': 400, 'temperature': 0, 'chat_template_kwargs': {'enable_thinking': False}}
vel = []
for _ in range(2):
    c, r, dt = req('/v1/chat/completions', cuerpo)
    vel.append(r['usage']['completion_tokens'] / dt)
directo = m5("for i in 1 2; do s=$(date +%s.%N); n=$(curl -s -m 300 http://127.0.0.1:18080/v1/chat/completions -H 'Content-Type: application/json' -d '"
            + json.dumps(cuerpo).replace("'", "") + "' | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"usage\"][\"completion_tokens\"])'); e=$(date +%s.%N); python3 -c \"print($n/($e-$s))\"; done")
d = [float(x) for x in directo.split()]
caso('sobrecoste-proxy', min(vel) > 0.9 * max(d), f'puerta {[round(v,1) for v in vel]} t/s vs directo {[round(v,1) for v in d]} t/s (extremo a extremo)')
# 4. modelo pequeno convive con qwen (capacity: 104 + 7 <= 118)
c, t, dt, u = chat('qwen3-8b', max_tokens=300)
rn = running()
caso('pequeno-convive', c == 200 and t and rn.get('qwen3.8-flash-next') == 'ready', f'{t[:20]!r} {dt:.1f}s running={rn}')
# 5. pedir DeepSeek por nombre: conmuta solo
c, t, dt, u = chat('deepseek-v4.1-flash', key=KD)
rn = running()
caso('conmuta-a-deepseek', c == 200 and t and 'qwen3.8-flash-next' not in rn and 'qwen3-8b' not in rn,
     f'{t[:20]!r} primera respuesta en {dt:.0f}s running={rn} centinela={m5("ls /etc/llama-guard.disable 2>/dev/null || echo no")}')
c, t, dt, u = chat('deepseek-v4.1-flash', key=KD)
caso('deepseek-ya-cargado', c == 200 and dt < 15, f'{t[:20]!r} {dt:.1f}s')
# 6. pedir qwen: vuelve solo y quita el centinela
c, t, dt, u = chat('qwen3.8-flash-next')
rn = running()
cent = m5('ls /etc/llama-guard.disable 2>/dev/null || echo no')
caso('vuelve-a-qwen', c == 200 and t and 'deepseek-v4.1-flash' not in rn and cent == 'no', f'{dt:.0f}s running={rn} centinela={cent}')
# 7. alguien para la unidad por fuera (lo que hace brush-server): llama-swap se entera
m5('sudo -n systemctl stop llama-flashnext')
time.sleep(25)
rn = running()
caso('parada-externa-detectada', 'qwen3.8-flash-next' not in rn, f'running={rn}')
# 8. con un job de brush vivo el wrapper se niega a cargar (simulado en dir temporal)
out = m5("d=$(mktemp -d); mkdir $d/j1; echo '{\"job_id\":\"j1\",\"state\":\"training\"}' > $d/j1/meta.json; "
         "BRUSH_JOBS_DIR=$d timeout 20 /usr/local/bin/m5-swap-run qwen; echo rc=$?; rm -rf $d; systemctl is-active llama-flashnext")
caso('brush-bloquea-carga', 'rc=1' in out and 'job de brush vivo' in out and out.endswith('inactive'), out.replace('\n', ' | '))
# 9. sin brush, la siguiente peticion lo vuelve a cargar
c, t, dt, u = chat('qwen3.8-flash-next')
caso('recarga-tras-parada', c == 200 and t, f'{dt:.0f}s running={running()}')
# 10. pausa / reanudar (API v268)
c1 = req('/api/models/pause', {}, method='POST')[0]
time.sleep(20)
rn = running(); cp = chat('qwen3.8-flash-next', max_tokens=8)[0]
c2 = req('/api/models/resume', {}, method='POST')[0]
c, t, dt, u = chat('qwen3.8-flash-next')
caso('pausa-reanuda', c1 == 200 and cp != 200 and c2 == 200 and c == 200, f'pause={c1} running={rn} chat_en_pausa={cp} resume={c2} chat={c} {dt:.0f}s')
print(f"\n{sum(o for _, o in res)}/{len(res)} casos OK")
