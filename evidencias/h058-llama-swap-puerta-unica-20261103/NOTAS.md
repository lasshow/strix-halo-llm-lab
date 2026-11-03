# H-058 — llama-swap v269 como puerta única del M5 (modelos de 100+ GiB que no conviven)

**Fecha:** 2026-11-03 · **Estado:** en producción. Rollback: `m5-swap-rollback` (deja el modo anterior: Gufo/Qwen en :8080 con clave y llama-swap solo para los modelos pequeños).

## Por qué llama-swap y no un conmutador propio

[llama-swap](https://github.com/mostlygeek/llama-swap) (≈6.200 estrellas, v269 del 2026-11-01)
ya resuelve "pedir un modelo por su nombre y que se cargue solo". Desde v26x trae el router
`capacity`: se declara la RAM total y lo que ocupa cada modelo, y descarga lo que haga falta
según `evict_cost`. Hacer otro conmutador no aportaba nada. **Lo que faltaba es una receta para
una máquina de 128 GB con modelos de 104-118 GiB que no caben juntos, servidos en contenedores
podman con systemd, y con otros procesos (brush, actualizador nocturno, vigía) que también
paran y arrancan esos motores.** Eso es lo que hay en `scripts/llama-swap/`.

Binario: `llama-swap_269_linux_amd64.tar.gz`, sha256 `933a45c5…f0f7cc`, igual que el
`checksums.txt` oficial. `llama-swap --validate` → `config is valid: 6 model(s)`.

## Diseño

```
clientes (Hermes, opencode, vigía) ──► :8080 llama-swap (clave) ──► 127.0.0.1:18080 Gufo Qwen / sin censura
                                                                └─► 127.0.0.1:18085 Gufo DeepSeek IQ1_M
                                                                └─► puertos 94xx: qwen3-8b, jev-2b, embeddings (llama-server)
```

- **Los modelos grandes no los lanza llama-swap.** Su `cmd` es `m5-swap-run <modelo>`, que se
  los pide a `m5-model` (sigue siendo la única autoridad de "un motor grande a la vez", del
  centinela y de llama-guard) y se queda vivo mientras la unidad systemd exista. Si alguien
  para la unidad por fuera (brush-server con `_liberar_ram`, el actualizador nocturno), el
  proceso termina y llama-swap marca el modelo como descargado. Así **no hubo que tocar
  brush-server ni llama-guard**.
- `cmdStop` = `m5-swap-stop`: para la unidad, borra el contenedor y quita el centinela.
- Con un job de brush vivo, `m5-swap-run` **se niega** a cargar un modelo grande (`m5-ocupado brush`).
- Las dos claves que ya había (Qwen y DeepSeek) valen en la puerta: los clientes no cambian de clave.
- `capacity`: pool `ram: 118`; Qwen 104, sin censura 112, DeepSeek 118, pequeños 1-7.
  Qwen + qwen3-8b caben juntos; DeepSeek echa a todos. Qwen `evict_cost: 100` y precargado al
  arrancar; DeepSeek y sin censura con `ttl: 3600` (1 h sin uso → se descargan y llama-guard
  devuelve Qwen).

## Pruebas (desde otro equipo de la LAN)

`scripts/llama-swap/e2e-puerta.py` → **12/13** en la primera pasada (`e2e-13-casos.txt`):

| Caso | Resultado |
|---|---|
| sin clave / clave falsa → 401 (también `/v1/models`) | OK |
| las dos claves valen | OK |
| sobrecoste del proxy | **ninguno medible**: 35,7-36,4 t/s por la puerta frente a 36,4 directo |
| qwen3-8b convive con Qwen | OK |
| pedir DeepSeek por nombre → conmuta solo | 34 s, centinela puesto |
| volver a Qwen | OK pero **583 s** → ver fallo 2 |
| parada externa de la unidad (como hace brush) | detectada, modelo descargado |
| job de brush vivo bloquea la carga | el test daba falso NO (leía solo stdout); repetido a mano: `rc=1`, `job de brush vivo (j1)` |
| recarga tras parada externa | 16 s |
| pausa / reanudar (API v268) | 503 en pausa, 200 al reanudar |

## Dos fallos encontrados y corregidos

1. **El vigía hacía conmutar el M5 cada 10 min.** Tomaba `data[0].id` de `/v1/models` para
   saber qué probar; con llama-swap esa lista trae todos los modelos y el primero (orden
   alfabético) es `deepseek-v4.1-flash`. Cada pasada pedía DeepSeek y echaba a Qwen. Ahora mira
   `/running` y prueba el modelo grande que esté *cargado*, o Qwen si no hay ninguno.
   **Lección general: detrás de un conmutador, `/v1/models` no dice qué está cargado.**
2. **Dos clientes a la vez con modelos grandes distintos atascaban la conmutación ~10 min.**
   Se reprodujo a propósito (`carrera.sh`: DeepSeek y, 3 s después, Qwen). La puerta
   descargaba DeepSeek mientras `m5-model deepseek` seguía esperando a que quedara sano; ese
   `m5-model` no terminaba hasta agotar su espera de 10 min, y llama-swap no arranca el
   siguiente hasta que termina el anterior. Arreglo: `m5-swap-stop` mata el `m5-model` de ese
   motor. Tras el arreglo, la misma carrera: **DeepSeek 200 en 34 s y Qwen 200 en 49 s**
   (`carrera-tras-fix.txt`), sin procesos colgados.

Conmutación limpia DeepSeek → Qwen sin competencia: **29 s** (journal).

## Pendiente / no probado

- El hook `preload` registra `failed to preload model qwen3.8-flash-next: status 404`, aunque
  el modelo sí queda cargado y sano. Sin investigar.
- El actualizador nocturno (`m5-engines-update`) sigue probando Qwen por :8080; ahora pasa por
  llama-swap. No se ha ejecutado una noche con este modo.
- Hermes: los proveedores de DeepSeek pasan de :8085 a :8080 (el mismo nombre de modelo). El
  cambio se aplica al reiniciar el gateway; no probado desde Hermes todavía.
