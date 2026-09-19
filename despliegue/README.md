# Despliegue de CASAMBI web

Guía de puesta en marcha y operación. La app corre en un contenedor detrás de un
túnel de Cloudflare: **el servidor no expone ningún puerto a internet**.

```
Internet ─▶ Cloudflare Access ─▶ túnel saliente ─▶ VPS
             (Google Workspace + MFA)               ├── cloudflared
                                                    └── gunicorn (1 worker, 8 hilos)
                                                        └── volumen /data
```

## Qué hay que contratar

| Qué | Dónde | Coste |
|---|---|---|
| VPS | Hetzner Cloud CX22, Alemania o Finlandia | ~5-6 €/mes con IVA |
| Dominio, aparte del corporativo | cualquier registrador | ~12 €/año |
| Cuenta Cloudflare, Zero Trust Free | cloudflare.com | 0 € (hasta 50 usuarios) |
| Copias | Backblaze B2 | 0 € (10 GB gratis; aquí se usan menos de 10 MB) |
| Identidad | Google Workspace, ya contratado | 0 € |

El dominio va **separado del de Impelsa a propósito**: Cloudflare exige tomar el
control del DNS del dominio que gestione, y hacerlo sobre el corporativo tocaría
los registros del correo de la empresa.

## Puesta en marcha

### 1. El servidor

```sh
# Como root, en un Debian/Ubuntu recién creado
apt update && apt install -y docker.io docker-compose-plugin git
adduser --disabled-password --gecos "" casambi
usermod -aG docker casambi
```

### 2. El código y la configuración

```sh
su - casambi
git clone <repo> casambi && cd casambi && git checkout web
cp despliegue/.env.deploy.example .env && chmod 600 .env
```

Rellenar `.env`. La clave se genera así:

```sh
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

> **Guarda `CASAMBI_SECRET_KEY` en el gestor de contraseñas de la empresa, fuera
> del servidor.** Sin ella no se pueden descifrar las credenciales de Casambi, ni
> las del servidor ni las de una copia de seguridad.

### 3. Cloudflare

1. Añadir el dominio a Cloudflare (cambia los servidores de nombres).
2. **Zero Trust → Networks → Tunnels**: crear un túnel, copiar su token a
   `TUNNEL_TOKEN`, y publicar la ruta `casambi.<dominio>` → `http://casambi:8000`.
3. **Zero Trust → Settings → Authentication**: añadir Google Workspace como
   proveedor.
4. **Zero Trust → Access → Applications**: crear una aplicación *self-hosted*
   para `casambi.<dominio>`, con una política que permita los correos
   `@impelsa.es`. Copiar el **Application Audience (AUD) tag** a
   `CASAMBI_ACCESS_AUD`, y el dominio del equipo
   (`<equipo>.cloudflareaccess.com`) a `CASAMBI_ACCESS_TEAM_DOMAIN`.

Opcionalmente, `CASAMBI_ACCESS_EMAILS` restringe además dentro de la app: una
segunda lista, por si la política de Access se relaja por error.

### 4. Arrancar

```sh
docker compose up -d --build
docker compose logs -f casambi
```

Comprobar desde el propio servidor que responde y que **sin JWT no deja entrar**:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/salud   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/        # 403
```

Ese 403 es lo que hay que ver: significa que quien alcance el origen sin pasar
por el túnel no entra. Luego, desde el navegador, `https://casambi.<dominio>`.

### 5. Las cuentas de Casambi

Con la app en marcha, **Cuentas y ajustes** → añadir las cuentas de Casambi de
la empresa. Se guardan cifradas en `/data/data/credentials.enc`.

### 6. Copias de seguridad

```sh
cp despliegue/copia.env.example despliegue/copia.env
chmod 600 despliegue/copia.env   # rellenar con las claves de Backblaze
crontab -e
# 0 3 * * *  /home/casambi/casambi/despliegue/copia.sh >> /home/casambi/copia.log 2>&1
```

**Ensayar una restauración antes de darlo por hecho.** `restaurar.sh` deja los
datos en un volumen aparte (`casambi-restaurado`), así que se puede comprobar sin
tocar producción:

```sh
despliegue/restaurar.sh
docker run --rm -v casambi-restaurado:/data -e CASAMBI_HOME=/data \
  -e CASAMBI_MODE=desktop -e CASAMBI_SECRET_KEY="$CASAMBI_SECRET_KEY" \
  casambi-web:dev python -c "import credentials; print(credentials.list_accounts())"
```

Si eso imprime las cuentas, la copia sirve. Si no, la copia no existe, diga lo
que diga el log del cron.

## Operación diaria

| Tarea | Orden |
|---|---|
| Ver el registro | `docker compose logs -f casambi` |
| Reiniciar | `docker compose restart casambi` |
| Actualizar el código | `git pull && docker compose up -d --build` |
| Estado del contenedor | `docker compose ps` |
| Copia a mano | `despliegue/copia.sh` |
| Listar copias | `despliegue/copia.sh` (las muestra al final) |

## Por qué un solo worker

`gunicorn -w 1 -k gthread --threads 8`, y no es un descuido. Las sesiones de
Casambi y la caché de redes viven en memoria del proceso y se comparten entre
todo el equipo, que es lo deseado con credenciales de empresa: una sola
autenticación y una sola descarga por red. Con varios workers habría una
autenticación y una caché por proceso, y la barra de progreso respondería desde
un worker distinto al que está descargando.

El `--timeout 300` tampoco es adorno: capturar una escena espera diez segundos a
que las luminarias apliquen el nivel, y generar un Excel con planos puede pasar
de treinta. Con el valor por defecto, gunicorn mataría el worker y con él la
caché de todos.

## Diagnóstico

| Síntoma | Causa probable |
|---|---|
| El contenedor sale con código 3 al arrancar | Falta `CASAMBI_SECRET_KEY` o las variables de Access. El log dice cuál |
| «No se pudo descifrar credentials.enc» | La `CASAMBI_SECRET_KEY` no es la que cifró el fichero |
| 403 en todo desde el navegador | La política de Access no incluye ese correo, o el AUD tag no coincide |
| 503 «No se puede verificar el acceso» | El servidor no alcanza el JWKS de Cloudflare. Se cierra a propósito: ante la duda no se deja pasar |
| Redirige siempre a /ajustes | No hay cuentas de Casambi configuradas |
| Un `.json.corrupto-<fecha>` en `/data/data` | Una escritura quedó a medias; ese fichero es el contenido anterior, recuperable a mano |
