#!/usr/bin/env bash
# Builds de llama.cpp VERSIONADAS, con symlink de promocion y vuelta atras.
#
# Por que existe (auditoria externa de la campana H-031, fallo D):
#   Al adoptar la build nueva, la campana hizo `git checkout` + `cmake --build`
#   ENCIMA de /models/llama.cpp, que es el arbol al que apunta el ExecStart de
#   produccion. Si el smoke posterior fallaba, el rollback restauraba la unidad
#   ... y la unidad restaurada seguia apuntando al MISMO path, ahora con el
#   binario NUEVO dentro. Restaurar la unidad no es restaurar la build: el
#   rollback existia sobre el papel y no revertia nada.
#
#   Aqui cada build vive en su propio directorio por sha completo y la
#   promocion es un cambio de symlink, que se deshace con `volver`.
#
#     <LLAMA_BUILDS_DIR>/<sha>/            arbol + build + manifest.json
#     <LLAMA_BUILDS_DIR>/ANTERIOR          sha al que apunta `volver`
#     <LLAMA_CURRENT> -> <LLAMA_BUILDS_DIR>/<sha>    symlink promovido
#
# NO TOCA NINGUNA UNIDAD. `promover` cambia el symlink y nada mas: ni
# daemon-reload, ni restart, ni escribe en /etc. La migracion del ExecStart de
# llama-flashnext al symlink se hara en otra ventana autorizada; esto solo deja
# el mecanismo preparado.
#
# Subcomandos:
#   construir <repo> <sha> [--patch fichero]   compila y registra el manifiesto
#   listar                                     builds registradas y cual manda
#   promover <sha>                             mueve el symlink (no reinicia)
#   volver                                     vuelve al sha de ANTERIOR
#
# Entorno (las pruebas los apuntan a un tmpdir para correr sin root):
#   LLAMA_BUILDS_DIR  por defecto /models/llama-builds
#   LLAMA_CURRENT     por defecto /models/llama-current
#   CMAKE_FLAGS       flags extra de configuracion
#   TRABAJOS          -j de la compilacion (por defecto nproc)
set -uo pipefail

DIR_BUILDS="${LLAMA_BUILDS_DIR:-/models/llama-builds}"
ACTUAL="${LLAMA_CURRENT:-/models/llama-current}"
ANTERIOR="${DIR_BUILDS}/ANTERIOR"
FLAGS_BASE="-DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF"
FLAGS="${CMAKE_FLAGS:-$FLAGS_BASE}"
TRABAJOS="${TRABAJOS:-$(nproc 2>/dev/null || echo 4)}"

muere() { echo "builds: $*" >&2; exit 1; }
info()  { echo "[builds] $*"; }

# SELinux: en el M5 esta en Enforcing y un binario fuera de /usr no arranca
# como servicio sin la etiqueta bin_t (H-002, 203/EXEC). La regla para
# /models/llama.cpp/build/bin ya existe; aqui se anade la equivalente para el
# arbol de builds versionadas. Las dos operaciones son idempotentes y se
# saltan en silencio donde no hay SELinux ni permisos (p. ej. las pruebas).
etiqueta_selinux() {
  local ruta="$1"
  command -v semanage >/dev/null 2>&1 || { info "sin semanage: no etiqueto"; return 0; }
  if ! semanage fcontext -l 2>/dev/null | grep -q "^${DIR_BUILDS}(/\.\*)?"; then
    sudo -n semanage fcontext -a -t bin_t "${DIR_BUILDS}(/.*)?" 2>/dev/null \
      && info "regla fcontext bin_t anadida para ${DIR_BUILDS}" \
      || info "no pude anadir la regla fcontext (sin sudo?): revisala a mano"
  fi
  command -v restorecon >/dev/null 2>&1 || return 0
  sudo -n restorecon -RF "$ruta" 2>/dev/null || restorecon -RF "$ruta" 2>/dev/null \
    || info "no pude ejecutar restorecon sobre ${ruta}"
}

sha256_de() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }

cmd_construir() {
  local repo="${1:-}" sha="${2:-}"; shift 2 || true
  local parche=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --patch) parche="${2:-}"; shift 2 ;;
      *) muere "argumento desconocido en construir: $1" ;;
    esac
  done
  [ -n "$repo" ] && [ -n "$sha" ] || muere "uso: builds.sh construir <repo> <sha> [--patch f]"
  [ -z "$parche" ] || [ -f "$parche" ] || muere "no encuentro el parche ${parche}"

  local destino="${DIR_BUILDS}/${sha}"
  if [ -x "${destino}/build/bin/llama-server" ]; then
    info "${sha} ya construida en ${destino}, no recompilo"
    echo "$destino"
    return 0
  fi
  mkdir -p "${destino}" || muere "no puedo crear ${destino}"

  info "clono ${repo} en ${destino}/src"
  if [ ! -d "${destino}/src/.git" ]; then
    git clone --quiet --no-checkout "$repo" "${destino}/src" \
      || muere "no pude clonar ${repo}"
  fi
  git -C "${destino}/src" fetch --quiet --all --tags 2>/dev/null
  # Un sha COMPLETO y un checkout detached: sin ramas moviles de por medio.
  git -C "${destino}/src" checkout --quiet --detach "$sha" \
    || muere "no pude posicionarme en ${sha}"
  local sha_real; sha_real=$(git -C "${destino}/src" rev-parse HEAD)
  local remoto;   remoto=$(git -C "${destino}/src" remote get-url origin 2>/dev/null)
  local rama;     rama=$(git -C "${destino}/src" describe --all --exact-match HEAD 2>/dev/null || echo "")

  local patch_sha=""
  if [ -n "$parche" ]; then
    patch_sha=$(sha256_de "$parche")
    info "aplico parche ${parche} (sha256 ${patch_sha:0:12})"
    git -C "${destino}/src" apply --whitespace=nowarn "$(realpath "$parche")" \
      || muere "el parche no aplica sobre ${sha_real}"
    cp "$parche" "${destino}/parche.diff"
  fi

  info "compilo (${TRABAJOS} trabajos): ${FLAGS}"
  # shellcheck disable=SC2086
  cmake -S "${destino}/src" -B "${destino}/build" $FLAGS \
      > "${destino}/build.log" 2>&1 \
    || { tail -20 "${destino}/build.log" >&2; muere "cmake configure fallo (ver ${destino}/build.log)"; }
  cmake --build "${destino}/build" --config Release -j "$TRABAJOS" -t llama-server \
      >> "${destino}/build.log" 2>&1 \
    || { tail -20 "${destino}/build.log" >&2; muere "la compilacion fallo (ver ${destino}/build.log)"; }

  etiqueta_selinux "$destino"

  local version; version=$("${destino}/build/bin/llama-server" --version 2>&1 | head -1)
  local compilador; compilador=$( (cc --version 2>/dev/null || gcc --version 2>/dev/null) | head -1)
  python3 - "${destino}/manifest.json" "$repo" "$remoto" "$sha_real" "$rama" \
           "$patch_sha" "$FLAGS" "$compilador" "$version" <<'PY'
import datetime, json, sys
ruta, repo, remoto, sha, rama, patch, flags, compilador, version = sys.argv[1:10]
json.dump({
    "repo": repo,
    "remoto": remoto or None,
    "sha": sha,
    "rama": rama or None,
    "patch_sha256": patch or None,
    "flags_cmake": flags,
    "compilador": compilador or None,
    "fecha": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "llama_server_version": version,
}, open(ruta, "w"), indent=1, ensure_ascii=False)
PY
  info "manifiesto en ${destino}/manifest.json"
  echo "$destino"
}

cmd_listar() {
  local promovida=""
  [ -L "$ACTUAL" ] && promovida=$(basename "$(readlink -f "$ACTUAL")")
  local ant=""; [ -f "$ANTERIOR" ] && ant=$(cat "$ANTERIOR")
  echo "builds en ${DIR_BUILDS} (promovida: ${promovida:-<ninguna>} · anterior: ${ant:-<ninguna>})"
  [ -d "$DIR_BUILDS" ] || return 0
  local d sha marca
  for d in "$DIR_BUILDS"/*/; do
    [ -d "$d" ] || continue
    sha=$(basename "$d")
    marca="  "; [ "$sha" = "$promovida" ] && marca="->"
    if [ -f "${d}manifest.json" ]; then
      python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
print("%s %s  %s  %s%s" % (
    sys.argv[2], m["sha"][:12], m.get("fecha", "?"),
    (m.get("llama_server_version") or "?")[:60],
    "  [parche %s]" % m["patch_sha256"][:8] if m.get("patch_sha256") else ""))' \
        "${d}manifest.json" "$marca"
    else
      echo "${marca} ${sha:0:12}  <sin manifiesto>"
    fi
  done
}

cmd_promover() {
  local sha="${1:-}"
  [ -n "$sha" ] || muere "uso: builds.sh promover <sha>"
  local destino="${DIR_BUILDS}/${sha}"
  [ -d "$destino" ] || muere "no hay build registrada para ${sha} en ${DIR_BUILDS}"

  # ANTERIOR se escribe ANTES de mover el symlink: si el proceso muere en
  # medio, `volver` sabe a donde ir. Al reves quedaba un symlink nuevo sin
  # rastro del viejo, que es justo el agujero del rollback de H-031.
  mkdir -p "$DIR_BUILDS"
  if [ -L "$ACTUAL" ]; then
    basename "$(readlink -f "$ACTUAL")" > "$ANTERIOR"
  else
    : > "$ANTERIOR"
  fi
  ln -sfn "$destino" "$ACTUAL" || muere "no pude mover el symlink ${ACTUAL}"
  info "promovida ${sha:0:12}: ${ACTUAL} -> ${destino}"
  info "la unidad productiva NO se ha tocado (ni daemon-reload ni restart)"
}

cmd_volver() {
  local ant=""
  [ -f "$ANTERIOR" ] && ant=$(cat "$ANTERIOR")
  if [ -z "$ant" ]; then
    muere "no hay build anterior registrada en ${ANTERIOR}: nada que deshacer"
  fi
  local destino="${DIR_BUILDS}/${ant}"
  [ -d "$destino" ] || muere "la build anterior ${ant} ya no esta en ${DIR_BUILDS}"
  # Se intercambian: `volver` dos veces deja el symlink donde estaba.
  local actual_sha=""
  [ -L "$ACTUAL" ] && actual_sha=$(basename "$(readlink -f "$ACTUAL")")
  ln -sfn "$destino" "$ACTUAL" || muere "no pude mover el symlink ${ACTUAL}"
  printf '%s\n' "$actual_sha" > "$ANTERIOR"
  info "vuelta a ${ant:0:12}: ${ACTUAL} -> ${destino}"
}

case "${1:-}" in
  construir) shift; cmd_construir "$@" ;;
  listar)    shift; cmd_listar "$@" ;;
  promover)  shift; cmd_promover "$@" ;;
  volver)    shift; cmd_volver "$@" ;;
  *) echo "uso: builds.sh {construir <repo> <sha> [--patch f]|listar|promover <sha>|volver}" >&2
     exit 1 ;;
esac
