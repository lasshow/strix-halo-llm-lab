#!/usr/bin/env python3
"""Bateria de calidad comparable Flash-Next vs Ornith-1.5-35B.
Mismos prompts, mismos parametros de muestreo, mismo endpoint OpenAI-compatible.
Uso: bateria.py <base_url> <etiqueta>   (key en ~/.secrets/m5-llama-api.key)
Salida: JSONL en resultados/<etiqueta>.jsonl
"""
import json, os, pathlib, sys, time, urllib.request

BASE = sys.argv[1].rstrip('/')
TAG = sys.argv[2]
KEY = pathlib.Path(os.path.expanduser('~/.secrets/m5-llama-api.key')).read_text().strip()
OUT = pathlib.Path(__file__).parent / 'resultados'
OUT.mkdir(exist_ok=True)

# (id, categoria, prompt, max_tokens, verificador)
PROMPTS = [
    ("py_primes", "codigo", "Escribe una funcion Python `primes_upto(n)` que devuelva la lista de primos <= n usando criba de Eratostenes. Solo el bloque de codigo, sin explicaciones.", 600, "python"),
    ("py_lru", "codigo", "Escribe una clase Python `LRUCache` con get(key) y put(key, value) en O(1), capacidad fija en el constructor. Solo codigo.", 900, "python"),
    ("rs_wordcount", "codigo", "Escribe una funcion Rust `pub fn word_count(s: &str) -> std::collections::HashMap<String, usize>` que cuente palabras en minusculas ignorando puntuacion. Solo el bloque de codigo.", 700, "rust"),
    ("ts_debounce", "codigo", "Escribe en TypeScript una funcion `debounce<T extends (...a:any[])=>void>(fn: T, ms: number): T` con tipos correctos y sin any en el retorno. Solo codigo.", 700, "typescript"),
    ("sql_join", "codigo", "Dadas las tablas facturas(id, proyecto_id, importe, fecha) y proyectos(id, nombre): escribe una consulta SQLite que devuelva nombre y suma de importes de 2026 por proyecto, ordenada descendente, solo proyectos con mas de 3 facturas. Solo SQL.", 400, "sqlite"),
    ("json_ficha", "json", "Devuelve SOLO un objeto JSON valido, sin markdown, con las claves exactas: nombre (string), capas (int), expertos_totales (int), expertos_activos (int), params_activos_b (float). Datos: modelo Ornith 1.5, 35.51B totales, 3B activos, 48 capas, 128 expertos de los que se usan 8.", 300, "json"),
    ("json_lista", "json", "Devuelve SOLO un array JSON de 4 objetos con claves id (int desde 1) y tarea (string), extraido de este texto: 'Primero medir prefill, luego medir generacion, despues comprobar calidad de codigo y por ultimo decidir si entra en produccion'. Sin texto adicional.", 300, "json"),
    ("instr_conteo", "instrucciones", "Responde exactamente con cinco palabras, ni una mas ni una menos, describiendo que es un modelo MoE.", 100, "manual"),
    ("instr_formato", "instrucciones", "Lista los planetas rocosos del sistema solar. Responde en castellano, en una sola linea, separados por punto y coma, sin numeracion y sin ninguna frase introductoria.", 150, "manual"),
    ("instr_negativa", "instrucciones", "Explica que es la memoria unificada en un APU. NO uses las palabras 'GPU', 'CPU' ni 'RAM' en ninguna parte de tu respuesta.", 400, "manual"),
    ("es_tecnico", "castellano", "Explica en 4 lineas y en castellano llano, para alguien no tecnico, por que un modelo con 35.000 millones de parametros de los que solo se usan 3.000 millones por palabra puede ir mas rapido que otro de 27.000 millones que usa todos.", 500, "manual"),
    ("es_redaccion", "castellano", "Reescribe este parrafo en tono profesional y sin redundancias, manteniendo todas las cifras: 'Pues hemos medido y la verdad es que el modelo nuevo va bastante mas rapido, o sea, saca 64 tokens por segundo generando y 1117 procesando, que comparado con el otro que hacia 25 y 225 es como el doble o mas en generacion y cinco veces en procesado.'", 500, "manual"),
    ("raz_mem", "razonamiento", "Un equipo tiene 124 GiB de memoria utiles. El modelo A ocupa 87 GiB y el modelo B 23,7 GiB. Ademas el sistema y la cache consumen 7 GiB. Razona paso a paso si pueden estar los dos cargados a la vez y cuanta memoria sobraria o faltaria.", 700, "manual"),
    ("raz_tasas", "razonamiento", "Un modelo genera 25,4 tokens/s y otro 64,46 tokens/s. Para una respuesta de 1.500 tokens, calcula el tiempo de cada uno en segundos con un decimal y cuantos segundos se ahorran. Muestra los calculos.", 500, "manual"),
]

HDRS = {'Content-Type': 'application/json', 'Authorization': f'Bearer {KEY}'}


def call(prompt, max_tokens):
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "top_p": 1.0,
        "seed": 1234,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(f'{BASE}/v1/chat/completions',
                                 data=json.dumps(body).encode(), headers=HDRS)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read())
    dt = time.time() - t0
    ch = d['choices'][0]
    usage = d.get('usage', {})
    return {
        'content': ch['message'].get('content') or '',
        'finish_reason': ch.get('finish_reason'),
        'segundos': round(dt, 2),
        'tok_salida': usage.get('completion_tokens'),
        'tok_entrada': usage.get('prompt_tokens'),
        'tps': round((usage.get('completion_tokens') or 0) / dt, 2) if dt else None,
    }


def main():
    path = OUT / f'{TAG}.jsonl'
    with path.open('w') as f:
        for pid, cat, prompt, mt, verif in PROMPTS:
            try:
                r = call(prompt, mt)
                err = None
            except Exception as e:
                r, err = {}, f'{type(e).__name__}: {e}'
            row = {'etiqueta': TAG, 'id': pid, 'categoria': cat,
                   'verificador': verif, 'max_tokens': mt, 'error': err, **r}
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
            f.flush()
            print(f"[{TAG}] {pid:<14} {r.get('segundos','-')}s "
                  f"{r.get('tok_salida','-')}tok {r.get('tps','-')}t/s "
                  f"fin={r.get('finish_reason')} vacio={not r.get('content')} {err or ''}",
                  flush=True)
    print(f'\nescrito {path}')


if __name__ == '__main__':
    main()
