#!/usr/bin/env python3
"""E2E del servicio DeepSeek (m5-model deepseek -> Gufo IQ1_M) tal como lo usan los clientes.
Uso: e2e_ds41.py <base_url> <fichero_clave>"""
import json, sys, time, urllib.request, urllib.error

base, kf = sys.argv[1], sys.argv[2]
KEY = open(kf).read().strip()
M = 'deepseek-v4.1-flash'
res = []

def post(path, body, key=KEY, timeout=600):
    h = {'Content-Type': 'application/json'}
    if key:
        h['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(base + path, json.dumps(body).encode(), h)
    t = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=timeout)); return 200, r, time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200], time.time() - t

def caso(nombre, ok, detalle):
    res.append((nombre, ok)); print(f"[{'OK ' if ok else 'NO '}] {nombre:28s} {detalle}", flush=True)

# 1. /v1/models con clave
req = urllib.request.Request(base + '/v1/models', headers={'Authorization': 'Bearer ' + KEY})
ids = [m['id'] for m in json.load(urllib.request.urlopen(req, timeout=20))['data']]
caso('models-nombre', M in ids, ids)
# 2. control negativo sin clave y con clave falsa
c1, _, _ = post('/v1/chat/completions', {'model': M, 'messages': [{'role': 'user', 'content': 'hola'}], 'max_tokens': 4}, key=None)
c2, _, _ = post('/v1/chat/completions', {'model': M, 'messages': [{'role': 'user', 'content': 'hola'}], 'max_tokens': 4}, key='clave-falsa')
caso('rechaza-sin-clave', c1 == 401 and c2 == 401, f'sin={c1} falsa={c2}')
# 3. chat sin razonamiento (lo que manda el vigia)
c, r, dt = post('/v1/chat/completions', {'model': M, 'messages': [{'role': 'user', 'content': 'Responde solo: OK'}], 'max_tokens': 600, 'chat_template_kwargs': {'enable_thinking': False}})
ch = r['choices'][0] if c == 200 else {}
txt = (ch.get('message', {}).get('content') or '').strip()
caso('chat-vigia', c == 200 and txt and ch.get('finish_reason') == 'stop', f'{c} {txt[:30]!r} fin={ch.get("finish_reason")} {dt:.1f}s')
# 4. razonamiento activado (como Hermes/opencode por defecto): trampa de las 3 maquinas
c, r, dt = post('/v1/chat/completions', {'model': M, 'messages': [{'role': 'user', 'content': 'Si tres maquinas tardan tres minutos en fabricar tres piezas, cuanto tardan cien maquinas en fabricar cien piezas? Responde con el numero de minutos.'}], 'max_tokens': 4096, 'reasoning_effort': 'high'})
msg = r['choices'][0]['message'] if c == 200 else {}
razon = msg.get('reasoning_content') or msg.get('reasoning') or ''
cont = msg.get('content') or ''
import re
caso('razonamiento-cot', c == 200 and re.search(r'\b(3|tres)\b', cont) and not re.search(r'\b100\b', cont), f'content={cont[:40]!r} razonamiento={len(razon)} car {dt:.1f}s')
# 5. tool calling
tools = [{'type': 'function', 'function': {'name': 'leer_temperatura', 'description': 'Lee la temperatura de una zona del horno',
          'parameters': {'type': 'object', 'properties': {'zona': {'type': 'string'}}, 'required': ['zona']}}}]
c, r, dt = post('/v1/chat/completions', {'model': M, 'tools': tools, 'messages': [{'role': 'user', 'content': 'Que temperatura hay en la zona 3 del horno? Usa la herramienta.'}], 'max_tokens': 1024, 'reasoning_effort': 'none'})
tc = (r['choices'][0]['message'].get('tool_calls') or []) if c == 200 else []
args = json.loads(tc[0]['function']['arguments']) if tc else {}
caso('tool-calling', bool(tc) and tc[0]['function']['name'] == 'leer_temperatura' and '3' in str(args.get('zona')), f'{tc[0]["function"] if tc else r if c != 200 else "sin tool_calls"}')
# 6. vuelta de la herramienta
if tc:
    msgs = [{'role': 'user', 'content': 'Que temperatura hay en la zona 3 del horno? Usa la herramienta.'}, r['choices'][0]['message'],
            {'role': 'tool', 'tool_call_id': tc[0].get('id', 'x'), 'content': '{"zona":"3","temperatura_c":847}'}]
    c, r2, dt = post('/v1/chat/completions', {'model': M, 'tools': tools, 'messages': msgs, 'max_tokens': 512, 'reasoning_effort': 'none'})
    t2 = r2['choices'][0]['message'].get('content') or '' if c == 200 else str(r2)
    caso('tool-resultado', '847' in t2, repr(t2[:60]))
# 7. dos conversaciones seguidas sin fuga (segundo cliente no ve el secreto del primero)
post('/v1/chat/completions', {'model': M, 'messages': [{'role': 'user', 'content': 'Recuerda esta palabra secreta: GIRASOL-7731. Contesta solo: vale'}], 'max_tokens': 16, 'reasoning_effort': 'none'})
c, r, dt = post('/v1/chat/completions', {'model': M, 'messages': [{'role': 'user', 'content': 'Cual es la palabra secreta que te dije antes? Si no lo sabes responde: NO LO SE'}], 'max_tokens': 32, 'reasoning_effort': 'none'})
t = r['choices'][0]['message'].get('content') or '' if c == 200 else ''
caso('sin-fuga-entre-chats', 'GIRASOL' not in t.upper(), repr(t[:50]))
print(f"\n{sum(o for _, o in res)}/{len(res)} casos OK")
