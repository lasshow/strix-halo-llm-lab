#!/usr/bin/env python3
"""H-057. Cliente de medida: mismo prompt, greedy, 512 tokens, streaming.
Mide tok/s de decode (entre primer y ultimo token) y tok/s de prefill (prompt / TTFT).
El prompt de edicion es un script bash real de ~2.400 tokens (EDIT_FILE).
Uso: h057-cliente-stream.py <brazo> <url_base> <clave|-> <modelo> <salida.jsonl>"""
import json, os, sys, time, urllib.request

brazo, base, clave, modelo, salida = sys.argv[1:6]
codigo = open(os.environ.get('EDIT_FILE', '/usr/local/bin/m5-model'), encoding='utf-8').read()
PROMPTS = {
    'prosa': 'Escribe un cuento de unas 400 palabras sobre un faro abandonado en la costa de Bizkaia.',
    'codigo': 'Escribe en Python un servidor HTTP minimo con asyncio (sin dependencias externas) que sirva ficheros estaticos de un directorio, con soporte de Range y Content-Length, y tests con unittest.',
    'edicion': 'Devuelve este script bash COMPLETO, identico salvo que cambies el puerto 8085 por 8086 en todas partes. Solo el codigo.\n\n```bash\n' + codigo + '\n```',
}

def una(nombre, prompt):
    body = {'model': modelo, 'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': 512, 'temperature': 0, 'stream': True,
            'stream_options': {'include_usage': True}, 'reasoning_effort': 'none',
            'chat_template_kwargs': {'enable_thinking': False, 'thinking': False}}
    h = {'Content-Type': 'application/json'}
    if clave != '-':
        h['Authorization'] = 'Bearer ' + clave
    req = urllib.request.Request(base + '/v1/chat/completions', json.dumps(body).encode(), h)
    t0 = time.time(); t1 = None; tl = None; n = 0; usage = None; texto = []
    with urllib.request.urlopen(req, timeout=1800) as r:
        for linea in r:
            linea = linea.decode('utf-8', 'replace').strip()
            if not linea.startswith('data:'):
                continue
            dato = linea[5:].strip()
            if dato == '[DONE]':
                break
            j = json.loads(dato)
            if j.get('usage'):
                usage = j['usage']
            for c in j.get('choices') or []:
                d = c.get('delta') or {}
                trozo = (d.get('content') or '') + (d.get('reasoning_content') or '') + (d.get('reasoning') or '')
                if trozo:
                    n += 1; tl = time.time(); t1 = t1 or tl; texto.append(trozo)
    ct = (usage or {}).get('completion_tokens') or n
    pt = (usage or {}).get('prompt_tokens')
    dec = (ct - 1) / (tl - t1) if t1 and tl and tl > t1 else None
    return {'brazo': brazo, 'prompt': nombre, 'prompt_tokens': pt, 'completion_tokens': ct,
            'ttft_s': round(t1 - t0, 2) if t1 else None, 'total_s': round(time.time() - t0, 2),
            'decode_tps': round(dec, 2) if dec else None,
            'prefill_tps': round(pt / (t1 - t0), 1) if pt and t1 else None,
            'gufo_tps': (usage or {}).get('completion_tokens_per_second'),
            'texto_inicio': ''.join(texto)[:160]}

with open(salida, 'a') as f:
    for nombre, p in PROMPTS.items():
        for rep in range(3):  # rep 0 = fria, se descarta al resumir
            try:
                r = una(nombre, p); r['rep'] = rep
            except Exception as e:
                r = {'brazo': brazo, 'prompt': nombre, 'rep': rep, 'error': str(e)[:300]}
            f.write(json.dumps(r, ensure_ascii=False) + '\n'); f.flush()
            print(json.dumps(r, ensure_ascii=False)[:300], flush=True)
