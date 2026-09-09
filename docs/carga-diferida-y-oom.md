# La flag que duplicó el prefill — y la que tumbó la máquina

Dos hallazgos del mismo día, encadenados: una opción por defecto de `llama.cpp` estaba
costando la mitad del rendimiento de prefill, y al corregirla salió a la luz un fallo de
configuración del servicio que dejaba el equipo entero inalcanzable.

---

## 1. `--lazy-mode off`: +92% de prefill, gratis

`llama.cpp` incorporó en 2026 la *carga diferida* de tensores (lazy loading). En `auto`
—el valor por defecto— la iGPU sale perdiendo. En este equipo el efecto es brutal.

**Medición A/B**, mismo binario, mismo modelo, mismo arranque, `-r 3`:

```
llama-bench -m Qwen3.8-Flash-Next-UD-IQ4_XS.gguf -ngl 99 -fa 1 -t 16 \
            -p 512 -n 128 -r 3 -lzm auto,off
```

| `lazy_mode` | pp512 (prefill) | tg128 (generación) |
|---|---:|---:|
| `auto` (por defecto) | 216,03 ± 0,81 t/s | 25,45 ± 0,25 t/s |
| **`off`** | **415,38 ± 4,43 t/s** | **27,68 ± 0,02 t/s** |
| | **+92%** | **+8,8%** |

El prefill casi se dobla. La generación mejora poco, y era de esperar: la generación está
limitada por ancho de banda de memoria, no por cómo se carguen los tensores.

### Por qué pasa

El fix que desactiva la carga diferida en iGPUs (`f3f1a8f`, PR #28326) entró
**diez horas después** del commit con el que estaba compilado el binario de producción
(`9113cc1`). Es decir: no era un fallo de configuración, era una regresión conocida que
simplemente no había llegado a esta build. La flag `-lzm` ya existía en el binario, así
que **no hizo falta recompilar**.

> **Cómo detectarlo sin leer el changelog:** compara tu rendimiento con el techo teórico
> de ancho de banda. Aquí los modelos densos rendían al 78–87% de su techo, pero el MoE
> se quedaba en el 19%. Esa asimetría es la señal: si un modelo va desproporcionadamente
> mal *respecto a sí mismo*, sospecha del software, no del hardware.

### Cómo comprobarlo en tu máquina

```bash
llama-bench -m <modelo.gguf> -ngl 99 -fa 1 -p 512 -n 128 -r 3 -lzm auto,off
```

Si `off` gana, ponlo en el servicio. Si no gana, tu build ya lleva el fix.

---

## 2. El OOM que dejó la máquina muerta pero respondiendo al ping

Al aplicar `--lazy-mode off` al servicio de producción, el equipo se volvió inalcanzable:
**el ping respondía, pero SSH rechazaba la conexión**. No se recuperó solo; hubo que
reiniciar físicamente.

### Qué pasó realmente

Con `lazy auto` los pesos van *mapeados a fichero*: si falta memoria, el kernel puede
descartar esas páginas y releerlas del disco. Con `lazy off` los 87 GiB se cargan
residentes y **dejan de ser reclamables**. Sumado al KV de 262.144 y a 24 GB de caché de
prompt, ya no cabía en 124 GB.

Pero lo que convirtió un OOM normal en una caída total fue esta línea de la unit:

```ini
OOMScoreAdjust=-500     # ← el problema
```

Eso le dice al kernel *«mata cualquier cosa antes que a este proceso»*. Y el kernel
obedeció al pie de la letra: fue matando `sshd`, `NetworkManager`, `systemd-resolved`,
`tailscaled`, `polkit`, `crond`, `auditd`... para salvar el servidor de inferencia.
De ahí el síntoma: el kernel seguía vivo (respondía al ping) pero no quedaba espacio de
usuario con el que conectarse.

Confirmado en el log:

```
Out of memory: Killed process 1871 (llama-server) ... oom_score_adj:-500
```

Al final murió igualmente, tras llevarse por delante el sistema entero.

### La corrección

| Parámetro | Antes | Después | Motivo |
|---|---|---|---|
| `OOMScoreAdjust` | `-500` | `500` | Que muera el modelo, **nunca** la máquina |
| `--cache-ram` | 24576 (24 GB) | 4096 (4 GB) | Con pesos residentes ya no sobran 24 GB |
| `--ubatch-size` | 4096 | 2048 | Buffers de cómputo más pequeños |

**La regla:** `OOMScoreAdjust` negativo en un servicio que consume el 80% de la RAM
convierte un fallo recuperable (el servicio muere y systemd lo reinicia) en uno que exige
presencia física. Un servidor de inferencia debe ser lo **primero** en morir, no lo último.

### Resultado verificado

```
LISTO tras 30 s   ·   oom_score_adj = 0   ·   NRestarts = 0
3 peticiones consecutivas: 27,2 / 27,4 / 27,2 t/s
presión de memoria: some avg10=0.00
```

Con `-c 262144 -np 2` la memoria se queda en ~108 GB de 124. Funciona y es estable, pero
el margen es estrecho: **no es sitio para un segundo modelo**.

---

## Lo que esto cambia en el resto del cuaderno

Las cifras anteriores de prefill de Qwen3.8-Flash-Next (225–300 t/s) se midieron con la
carga diferida activa. **Están todas infravaloradas.** El CSV incorpora ahora una columna
`commit` y otra `lazy_mode` precisamente para que esto no vuelva a pasar: un número de
rendimiento sin el commit que lo produjo no es reproducible.

Lección metodológica, hermana de la del `ubatch`: **una medición sin la versión exacta del
software es una anécdota, no un dato.**
