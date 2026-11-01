import json, urllib.request, uuid
K = [l.strip() for l in open('/etc/llama-server/api-keys.txt') if l.strip() and not l.startswith('#')][0]
P = ("Explica en dos frases que es un termopar tipo K. Prohibido usar las "
     "palabras 'temperatura', 'calor' y 'grados'.")
def pide(prompt):
    b = {"model": "qwen3.8-flash-next", "temperature": 0, "max_tokens": 400,
         "messages": [{"role": "user", "content": prompt}], "chat_template_kwargs": {"enable_thinking": False}}
    r = urllib.request.Request('http://127.0.0.1:8080/v1/chat/completions', data=json.dumps(b).encode(),
                               headers={'Authorization': 'Bearer ' + K, 'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(r, timeout=300))["choices"][0]["message"]["content"]
casos = [("sin run-id (igual que H-053)", P)] + [(f"run-id {i}", f"[run-id: {uuid.uuid4().hex[:8]}]\n" + P) for i in range(5)]
malas = 0
for n, p in casos:
    t = pide(p); bad = [w for w in ("temperatura", "calor", "grados") if w in t.lower()]
    malas += bool(bad)
    print(("NO " if bad else "OK ") + n, bad, "|", t[:140].replace("\n", " "))
print(f"fallos {malas}/{len(casos)}")
