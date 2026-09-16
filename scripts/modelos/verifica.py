#!/usr/bin/env python3
"""Verifica las respuestas de bateria.py: compila/ejecuta codigo, valida JSON,
y comprueba las reglas manuales objetivas. Uso: verifica.py <etiqueta> [...]"""
import json, pathlib, re, subprocess, sys, tempfile, os

RES = pathlib.Path(__file__).parent / 'resultados'


def bloque(txt):
    m = re.findall(r'```[a-zA-Z]*\n(.*?)```', txt, re.S)
    return m[0] if m else txt


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)
    return p.returncode, (p.stdout + p.stderr)[-400:]


def v_python(pid, code):
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        extra = {
            'py_primes': "\nassert primes_upto(30)==[2,3,5,7,11,13,17,19,23,29], primes_upto(30)\nassert primes_upto(1)==[]\nprint('OK')\n",
            'py_lru': "\nc=LRUCache(2)\nc.put(1,1); c.put(2,2)\nassert c.get(1)==1\nc.put(3,3)\nassert c.get(2) in (-1,None)\nassert c.get(3)==3\nprint('OK')\n",
        }.get(pid, "\nprint('OK')\n")
        f.write(code + extra)
        p = f.name
    rc, out = run(['python3', p])
    os.unlink(p)
    return rc == 0 and 'OK' in out, out


def v_rust(pid, code):
    d = tempfile.mkdtemp()
    src = os.path.join(d, 'l.rs')
    open(src, 'w').write(code + """
fn main(){
 let m = word_count("Hola, hola mundo! El mundo.");
 assert_eq!(m.get("hola"), Some(&2usize));
 assert_eq!(m.get("mundo"), Some(&2usize));
 println!("OK");
}
""")
    rc, out = run(['rustc', '-o', os.path.join(d, 'l'), src])
    if rc != 0:
        return False, out
    rc, out = run([os.path.join(d, 'l')])
    return rc == 0 and 'OK' in out, out


def v_typescript(pid, code):
    d = tempfile.mkdtemp()
    src = os.path.join(d, 'l.ts')
    open(src, 'w').write(code)
    tsc = pathlib.Path(__file__).parent / 'node_modules/.bin/tsc'
    rc, out = run([str(tsc), '--noEmit', '--strict', src])
    return rc == 0, out


def v_sqlite(pid, code):
    d = tempfile.mkdtemp()
    db = os.path.join(d, 'x.db')
    setup = """CREATE TABLE proyectos(id INTEGER, nombre TEXT);
CREATE TABLE facturas(id INTEGER, proyecto_id INTEGER, importe REAL, fecha TEXT);
INSERT INTO proyectos VALUES(1,'A'),(2,'B');
INSERT INTO facturas VALUES(1,1,10,'2026-01-01'),(2,1,20,'2026-02-01'),(3,1,30,'2026-03-01'),(4,1,40,'2026-04-01'),(5,2,99,'2026-01-01');
"""
    rc, out = run(['sqlite3', db], input=setup)
    rc, out = run(['sqlite3', db], input=code if code.strip().endswith(';') else code + ';')
    ok = rc == 0 and 'A|100' in out.replace('A|100.0', 'A|100') and 'B|' not in out
    return ok, out


def v_json(pid, txt):
    t = bloque(txt).strip()
    try:
        obj = json.loads(t)
    except Exception as e:
        return False, f'no parsea: {e}'
    if pid == 'json_ficha':
        need = {'nombre', 'capas', 'expertos_totales', 'expertos_activos', 'params_activos_b'}
        if not isinstance(obj, dict) or set(obj) != need:
            return False, f'claves {sorted(obj) if isinstance(obj, dict) else type(obj)}'
        ok = obj['capas'] == 48 and obj['expertos_totales'] == 128 and obj['expertos_activos'] == 8 and abs(float(obj['params_activos_b']) - 3) < 0.01
        return ok, json.dumps(obj, ensure_ascii=False)
    if pid == 'json_lista':
        ok = isinstance(obj, list) and len(obj) == 4 and all(set(o) == {'id', 'tarea'} for o in obj) and [o['id'] for o in obj] == [1, 2, 3, 4]
        return ok, json.dumps(obj, ensure_ascii=False)[:200]
    return True, ''


def v_manual(pid, txt):
    t = txt.strip()
    low = t.lower()
    if pid == 'instr_conteo':
        n = len(t.split())
        return n == 5, f'{n} palabras: {t[:80]}'
    if pid == 'instr_formato':
        una = len([l for l in t.splitlines() if l.strip()]) == 1
        pl = all(p in low for p in ('mercurio', 'venus', 'tierra', 'marte'))
        return una and pl and ';' in t, t[:120]
    if pid == 'instr_negativa':
        prohib = [w for w in ('gpu', 'cpu', 'ram') if re.search(rf'\b{w}\b', low)]
        return not prohib, f'prohibidas={prohib}'
    if pid == 'raz_mem':
        # 87 + 23,7 + 7 = 117,7 <= 124 -> SI caben, sobran 6,3 GiB
        num = re.sub(r'[^0-9,.]', '', t)
        tiene_total = '117,7' in t or '117.7' in t
        tiene_sobra = '6,3' in t or '6.3' in t
        return tiene_total and tiene_sobra, t[-200:]
    if pid == 'raz_tasas':
        return '59' in t and '23' in t, t[-200:]
    return None, t[:150]


V = {'python': v_python, 'rust': v_rust, 'typescript': v_typescript,
     'sqlite': v_sqlite, 'json': v_json, 'manual': v_manual}


def main():
    for tag in sys.argv[1:]:
        p = RES / f'{tag}.jsonl'
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        out = []
        auto_ok = auto_n = 0
        for r in rows:
            c = r.get('content') or ''
            f = V[r['verificador']]
            code = bloque(c) if r['verificador'] in ('python', 'rust', 'typescript', 'sqlite') else c
            try:
                ok, det = f(r['id'], code)
            except Exception as e:
                ok, det = False, f'{type(e).__name__}: {e}'
            r['ok'] = ok
            r['detalle'] = det
            if ok is not None:
                auto_n += 1
                auto_ok += bool(ok)
            out.append(r)
            print(f"{tag:<10} {r['id']:<14} {'OK ' if ok else ('?  ' if ok is None else 'FAIL')} "
                  f"{r.get('segundos')}s {r.get('tps')}t/s  {str(det)[:90]}")
        (RES / f'{tag}.verificado.jsonl').write_text(
            '\n'.join(json.dumps(r, ensure_ascii=False) for r in out))
        print(f'--- {tag}: {auto_ok}/{auto_n} verificables OK\n')


if __name__ == '__main__':
    main()
