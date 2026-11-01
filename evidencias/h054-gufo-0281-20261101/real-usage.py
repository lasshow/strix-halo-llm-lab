#!/usr/bin/env python3
"""Banco de carga real (mismas 6 tareas y prompts que H-049, corpus del commit a91ff6a)
contra la produccion del M5, leyendo velocidades de `usage` (Gufo >= 0.2x las devuelve).
Uso: real-usage.py <dir_corpus> <salida.jsonl>"""
import importlib.util, json, sys, time, urllib.request
from pathlib import Path

corpus = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("br", corpus / "scripts/bench-real.py")
br = importlib.util.module_from_spec(spec); spec.loader.exec_module(br)
K = [l.strip() for l in open('/etc/llama-server/api-keys.txt') if l.strip() and not l.startswith('#')][0]


def mide(prompt):
    body = {"model": "qwen3.8-flash-next", "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400, "temperature": 0.0, "top_k": 1,
            "chat_template_kwargs": {"enable_thinking": False}}
    r = urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions", data=json.dumps(body).encode(),
                               headers={"Authorization": "Bearer " + K, "Content-Type": "application/json"})
    t0 = time.monotonic(); d = json.load(urllib.request.urlopen(r, timeout=1800)); wall = time.monotonic() - t0
    u = d["usage"]
    acc = 100.0 * u.get("draft_tokens_accepted", 0) / u["draft_tokens"] if u.get("draft_tokens") else float("nan")
    return dict(prompt_total=u["prompt_tokens"], cached=u.get("cached_tokens", 0),
                pp=round(u.get("prompt_tokens_per_second", 0), 1), tg=round(u.get("completion_tokens_per_second", 0), 2),
                gen=u["completion_tokens"], acept=round(acc, 1), wall=round(wall, 2),
                finish=d["choices"][0].get("finish_reason"))


with open(sys.argv[2], "w") as out:
    for t in br.TAREAS:
        prompt = br.construye_prompt(t) if hasattr(br, "construye_prompt") else None
        if prompt is None:
            texto = (corpus / t["fichero"]).read_text()
            prompt = t["instruccion"] + "\n\n```\n" + texto + "\n```"
        c = mide(prompt)
        print(f"[*] {t['id']} frio: prompt={c['prompt_total']} pp={c['pp']} tg={c['tg']}", flush=True)
        for i in (1, 2):
            m = mide(prompt)
            print(f"    pasada {i}: cache={m['cached']} tg={m['tg']} gen={m['gen']} acept={m['acept']}% wall={m['wall']}s", flush=True)
            out.write(json.dumps(dict(id=t["id"], clase=t["clase"], pasada=i, frio=c, **m)) + "\n")
