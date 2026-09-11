#!/usr/bin/env bash
# Credencial de una unidad de llama-server, derivada del ExecStart EFECTIVO.
#
# Por que existe (auditoria externa de la campana H-031, fallo A):
#   restauracion.sh leia la clave de `LLAMA_API_KEY=` en /etc/llama-server/*.env,
#   pero la unidad productiva arranca con `--api-key-file /etc/llama-server/...`.
#   Hoy los dos ficheros coinciden POR CASUALIDAD (queda un .env residual de una
#   instalacion anterior). El dia que se rote la clave en el fichero que usa el
#   servidor y no en el .env, el gate de restauracion se pondria rojo con
#   produccion sana; el dia que se rote al reves, se pondria VERDE con el
#   servidor sirviendo otra clave. Un gate que mira una fuente distinta de la
#   que usa el servicio no comprueba el servicio.
#
#   La unica fuente valida es la linea de arranque que systemd ejecuta de
#   verdad: `systemctl cat <unidad>` (que ya incluye los drop-ins).
#
# LA CLAVE NO SE IMPRIME. La funcion no escribe nada en stdout: deja el valor
# en la variable CREDENCIAL y el origen en CREDENCIAL_ORIGEN. Asi no puede
# colarse en un log por un `set -x`, un pipe o una captura despistada. Ejecutado
# como programa muestra solo la forma ENMASCARADA; hace falta --crudo para que
# emita el valor, y eso solo lo usan otros scripts que van a ponerlo en el
# entorno del proceso hijo.
#
# Uso como biblioteca:
#     . "$(dirname "$0")/credencial.sh"
#     credencial_de_unidad llama-flashnext || exit 1
#     echo "origen: ${CREDENCIAL_ORIGEN} ($(enmascara "$CREDENCIAL"))"
#
# Uso como programa:
#     credencial.sh llama-flashnext           # imprime origen + clave enmascarada
#     credencial.sh --crudo llama-flashnext   # imprime la clave (para capturarla)
#
# SYSTEMCTL permite sustituir el binario de systemd; existe para que las
# pruebas puedan montar una unidad falsa sin systemd ni root.

# Salidas de credencial_de_unidad. CREDENCIAL_ORIGEN es "--api-key-file <ruta>"
# o "--api-key", y se imprime en los gates para que quede dicho de donde sale.
CREDENCIAL=""
CREDENCIAL_ORIGEN=""

enmascara() {
  # abc...xy — suficiente para cotejar dos claves de un vistazo sin revelarla.
  local v="${1:-}"
  if [ "${#v}" -lt 8 ]; then
    printf '<clave de %d caracteres>' "${#v}"
  else
    printf '%s...%s' "${v:0:3}" "${v: -2}"
  fi
}

# Linea ExecStart efectiva de la unidad, con las continuaciones unidas.
# Si hay varias (unidad base + drop-ins) gana la ULTIMA no vacia, que es la
# regla de systemd: un `ExecStart=` vacio resetea y lo que venga despues manda.
_execstart_efectivo() {
  local linea acum="" ultimo="" continua=0
  while IFS= read -r linea; do
    if [ "$continua" = 1 ]; then
      acum+=" ${linea%\\}"
      case "$linea" in *\\) ;; *) ultimo="$acum"; continua=0 ;; esac
      continue
    fi
    case "$linea" in
      ExecStart=*) ;;
      *) continue ;;
    esac
    acum="${linea#ExecStart=}"
    # prefijos de systemd delante del binario: + - @ ! :
    while [ -n "$acum" ]; do
      case "$acum" in
        [-+@!:]*) acum="${acum#?}" ;;
        *) break ;;
      esac
    done
    case "$linea" in
      *\\) acum="${acum%\\}"; continua=1 ;;
      *)   [ -n "${acum// }" ] && ultimo="$acum" ;;
    esac
  done
  printf '%s\n' "$ultimo"
}

credencial_de_unidad() {
  local unidad="${1:-}"
  CREDENCIAL=""
  CREDENCIAL_ORIGEN=""
  if [ -z "$unidad" ]; then
    echo "credencial: falta el nombre de la unidad" >&2
    return 1
  fi

  # Un fichero de unidad legible tiene una seccion [Service]. `systemctl cat`
  # sin privilegios sobre una unidad 600 imprime la cabecera "# /etc/..." y
  # falla en el cuerpo con "Permiso denegado": texto NO vacio pero inutil.
  # Con la comprobacion "-z" el sudo nunca llegaba a intentarse (visto al
  # lanzar H-032 en el M5 real, con los tests en verde).
  local texto
  texto=$("${SYSTEMCTL:-systemctl}" cat "$unidad" 2>/dev/null)
  if ! printf '%s\n' "$texto" | grep -q '^\[Service\]'; then
    texto=$(sudo -n "${SYSTEMCTL:-systemctl}" cat "$unidad" 2>/dev/null)
  fi
  if ! printf '%s\n' "$texto" | grep -q '^\[Service\]'; then
    echo "credencial: no pude leer la unidad '${unidad}' con systemctl cat" >&2
    return 1
  fi

  # Fuente principal: `systemctl show -p ExecStart`, que devuelve el argv YA
  # RESUELTO por systemd (continuaciones, comillas, drop-ins). Es lo que el
  # proceso ejecuta de verdad y no requiere privilegios. Raspar `systemctl cat`
  # queda como respaldo: en el M5 real recortaba la barra de continuacion de
  # algunas lineas y el parser cortaba el ExecStart a la mitad.
  local arranque=""
  local show
  show=$("${SYSTEMCTL:-systemctl}" show -p ExecStart --value "$unidad" 2>/dev/null)
  case "$show" in
    *'argv[]='*)
      arranque="${show#*argv\[\]=}"
      arranque="${arranque%% ; ignore_errors=*}"
      ;;
  esac
  if [ -z "${arranque// }" ]; then
    arranque=$(printf '%s\n' "$texto" | _execstart_efectivo)
  fi
  if [ -z "${arranque// }" ]; then
    echo "credencial: la unidad '${unidad}' no declara ExecStart" >&2
    return 1
  fi

  local -a toks
  # Sin globbing: un argumento con '*' no puede expandirse contra el disco.
  set -f
  # shellcheck disable=SC2206
  toks=($arranque)
  set +f

  local i ruta="" valor=""
  for ((i = 0; i < ${#toks[@]}; i++)); do
    case "${toks[i]}" in
      --api-key-file)   ruta="${toks[i+1]:-}" ;;
      --api-key-file=*) ruta="${toks[i]#*=}" ;;
      --api-key)        valor="${toks[i+1]:-}" ;;
      --api-key=*)      valor="${toks[i]#*=}" ;;
    esac
  done

  # --api-key-file manda sobre --api-key: si la unidad trae los dos, el fichero
  # es el que puede rotarse sin tocar la unidad, y es el que gana en llama.cpp.
  if [ -n "$ruta" ]; then
    local contenido
    contenido=$(cat "$ruta" 2>/dev/null) || contenido=""
    if [ -z "$contenido" ]; then
      contenido=$(sudo -n cat "$ruta" 2>/dev/null) || contenido=""
    fi
    # Primer campo no vacio: el fichero puede traer varias claves, una por linea.
    local k
    k=$(printf '%s\n' "$contenido" | awk 'NF {print $1; exit}')
    if [ -z "$k" ]; then
      echo "credencial: '${unidad}' usa --api-key-file ${ruta} pero no pude leer ninguna clave de ahi" >&2
      return 1
    fi
    CREDENCIAL="$k"
    CREDENCIAL_ORIGEN="--api-key-file ${ruta}"
    return 0
  fi

  if [ -n "$valor" ]; then
    CREDENCIAL="$valor"
    CREDENCIAL_ORIGEN="--api-key"
    return 0
  fi

  echo "credencial: el ExecStart de '${unidad}' no trae ni --api-key-file ni --api-key" >&2
  return 1
}

# Ejecutado como programa (no sourced).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  set -uo pipefail
  crudo=0
  if [ "${1:-}" = "--crudo" ]; then crudo=1; shift; fi
  credencial_de_unidad "${1:-}" || exit 1
  if [ "$crudo" = 1 ]; then
    printf '%s\n' "$CREDENCIAL"
  else
    printf 'origen: %s · clave: %s\n' "$CREDENCIAL_ORIGEN" "$(enmascara "$CREDENCIAL")"
  fi
fi
