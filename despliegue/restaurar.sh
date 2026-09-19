#!/bin/sh
# Restaura el volumen de datos de CASAMBI desde una copia.
#
#   despliegue/restaurar.sh                 # la copia más reciente, a un volumen de prueba
#   despliegue/restaurar.sh <id-snapshot>   # una concreta
#   CASAMBI_VOLUMEN_DESTINO=casambi_casambi-data despliegue/restaurar.sh   # en serio
#
# Por defecto restaura a un volumen APARTE (casambi-restaurado) para poder
# comprobar el contenido sin tocar lo que está en producción. Pasar el volumen
# real como destino es una decisión deliberada.
#
# Recuerda que además de la contraseña de restic hace falta la CASAMBI_SECRET_KEY
# original para que el fichero de credenciales restaurado se pueda descifrar.

set -eu

SNAPSHOT="${1:-latest}"
DESTINO="${CASAMBI_VOLUMEN_DESTINO:-casambi-restaurado}"
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

if [ "$DESTINO" = "casambi_casambi-data" ]; then
  printf 'Vas a sobrescribir el volumen de PRODUCCIÓN. Escribe «sí» para seguir: '
  read -r respuesta
  [ "$respuesta" = "sí" ] || { echo "Cancelado."; exit 1; }
fi

docker volume create "$DESTINO" >/dev/null

echo "Restaurando $SNAPSHOT en el volumen $DESTINO"
docker run --rm \
  -e RESTIC_REPOSITORY -e RESTIC_PASSWORD \
  -e B2_ACCOUNT_ID -e B2_ACCOUNT_KEY \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  -v "$DESTINO:/destino" \
  -v restic-cache:/root/.cache/restic $MONTAJE_REPO \
  restic/restic:latest \
  restore "$SNAPSHOT" --target /destino --include /datos

# restic recrea la ruta completa (/datos/...), así que se aplana al destino.
docker run --rm -v "$DESTINO:/v" alpine:3 sh -c '
  if [ -d /v/datos ]; then
    cp -a /v/datos/. /v/ && rm -rf /v/datos
  fi'

echo
echo "Contenido restaurado en $DESTINO:"
docker run --rm -v "$DESTINO:/v:ro" alpine:3 sh -c '
  find /v -maxdepth 2 -type f | head -20
  echo "---"
  echo "ficheros: $(find /v -type f | wc -l)"'
