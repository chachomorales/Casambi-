#!/bin/sh
# Copia de seguridad del volumen de datos de CASAMBI.
#
# Respalda anotaciones, planos, informes y el fichero de credenciales cifrado.
# Son menos de diez megas, pero es todo el trabajo de anotación del equipo y no
# existe en ningún otro sitio: la nube de Casambi no guarda nada de esto.
#
# Se ejecuta desde el host, en cron:
#   0 3 * * *  /ruta/despliegue/copia.sh >> /var/log/casambi-copia.log 2>&1
#
# Variables (en despliegue/copia.env, modo 0600):
#   RESTIC_REPOSITORY   p. ej. b2:mi-cubo:casambi
#   RESTIC_PASSWORD     contraseña del repositorio restic
#   B2_ACCOUNT_ID       clave de aplicación de Backblaze
#   B2_ACCOUNT_KEY
#
# OJO: la contraseña de restic y la CASAMBI_SECRET_KEY son dos cosas distintas y
# hacen falta LAS DOS para recuperar. Sin la segunda, las credenciales
# restauradas no se pueden descifrar. Guarda ambas en el gestor de contraseñas.

set -eu

VOLUMEN="${CASAMBI_VOLUMEN:-casambi_casambi-data}"
AQUI="$(cd "$(dirname "$0")" && pwd)"
ENTORNO="${CASAMBI_COPIA_ENV:-$AQUI/copia.env}"

[ -f "$ENTORNO" ] || { echo "Falta $ENTORNO" >&2; exit 1; }
# `set -a` exporta todo lo que defina el fichero: `docker run -e VAR` solo pasa
# las variables que estén en el entorno, no las del shell, y sin eso restic se
# queda sin contraseña y falla con un «el repositorio no existe» que despista.
set -a
# shellcheck disable=SC1090
. "$ENTORNO"
set +a

: "${RESTIC_REPOSITORY:?falta en copia.env}"
: "${RESTIC_PASSWORD:?falta en copia.env}"

# Un repositorio en disco local (segunda copia, o para ensayar una restauración)
# necesita que el directorio esté montado dentro del contenedor de restic.
MONTAJE_REPO=""
if [ -n "${RESTIC_REPO_DIR:-}" ]; then
  mkdir -p "$RESTIC_REPO_DIR"
  MONTAJE_REPO="-v $RESTIC_REPO_DIR:/repo"
  RESTIC_REPOSITORY="/repo"
  export RESTIC_REPOSITORY
fi

docker volume inspect "$VOLUMEN" >/dev/null 2>&1 || {
  echo "El volumen $VOLUMEN no existe" >&2; exit 1; }

restic_en_contenedor() {
  docker run --rm \
    -e RESTIC_REPOSITORY -e RESTIC_PASSWORD \
    -e B2_ACCOUNT_ID -e B2_ACCOUNT_KEY \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
    -v "$VOLUMEN:/datos:ro" \
    -v restic-cache:/root/.cache/restic $MONTAJE_REPO \
    restic/restic:latest "$@"
}

# La primera vez hay que crear el repositorio. Se distingue «ya existe» de un
# fallo real: silenciar los dos dejaba que un error de contraseña se manifestase
# después como un desconcertante «el repositorio no existe».
if ! restic_en_contenedor cat config >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] creando el repositorio"
  restic_en_contenedor init
fi

echo "[$(date '+%F %T')] copiando $VOLUMEN"
restic_en_contenedor backup /datos --tag casambi --host casambi

echo "[$(date '+%F %T')] aplicando política de retención"
restic_en_contenedor forget --tag casambi \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune

# Comprobar que lo guardado se puede leer. Una copia que no se verifica no es
# una copia: es un directorio con la conciencia tranquila.
echo "[$(date '+%F %T')] verificando"
restic_en_contenedor check --read-data-subset=10%

echo "[$(date '+%F %T')] listo"
restic_en_contenedor snapshots --tag casambi --compact | tail -5
