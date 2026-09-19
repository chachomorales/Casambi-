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
| VPS | Hetzner Cloud, **Ashburn (Virginia)**, x86 con 4 GB | ~5-9 $/mes |
| Dominio, aparte del corporativo | cualquier registrador | ~12 €/año |
| Cuenta Cloudflare, Zero Trust Free | cloudflare.com | 0 € (hasta 50 usuarios) |
| Copias | Backblaze B2 | 0 € (10 GB gratis; aquí se usan menos de 10 MB) |
| Identidad | Google Workspace, ya contratado | 0 € |

**La ubicación es Ashburn, Virginia**, no Europa. El equipo y los clientes están
en Guatemala: desde allí Ashburn queda a unos 50-70 ms y Alemania a 160-200 ms,
al mismo precio. Cloudflare amortigua parte de esa diferencia —el tráfico entra
por su punto de presencia local y viaja por su troncal—, pero no hay ninguna
razón para elegir el origen lejano.

Consecuencia práctica: **los servidores Arm (CAX) de Hetzner son solo europeos**,
así que en EEUU se usa un x86 de la serie CPX. Da igual para el despliegue,
porque la imagen se construye en el propio servidor.

El dominio va **separado del de Impelsa a propósito**: Cloudflare exige tomar el
control del DNS del dominio que gestione, y hacerlo sobre el corporativo tocaría
los registros del correo de la empresa.

### Contratar el VPS, paso a paso

1. Cuenta en <https://accounts.hetzner.com>. Registrarse como empresa si se
   quiere la factura a nombre de Impelsa. Guatemala está fuera de la UE, así que
   no hay IVA alemán ni nada que declarar por ese lado.
2. Hetzner pide **verificación de identidad** en cuentas nuevas; tarda de horas a
   un par de días. Conviene hacerlo antes que nada.
3. <https://console.hetzner.cloud> → New Project (`casambi`) → Add Server:

   | Campo | Valor |
   |---|---|
   | Location | **Ashburn, Virginia** (lo más cerca de Guatemala) |
   | Image | Ubuntu 24.04 LTS |
   | Type | x86, serie CPX, **con 4 GB de RAM** |
   | Networking | dejar la IPv4 pública (para el SSH) |
   | SSH keys | añadir la clave pública; nunca contraseña |
   | Firewalls | uno que solo permita el 22. Con el túnel no hace falta abrir más |

**Los 4 GB son lo que no conviene recortar.** `cobertura.parse_proyecto` mantiene
descomprimidos a la vez todos los planos de un proyecto multinivel; el tope de 20
niveles (`config.MAX_NIVELES_COBERTURA`) acota el peor caso, pero el margen con
2 GB es escaso y la diferencia de precio son un par de dólares.

Las dependencias nativas —Pillow, PyMuPDF, cryptography— traen ruedas manylinux
para x86-64 y para aarch64, así que la arquitectura no condiciona nada: si algún
día se vuelve a Europa, un CAX (Arm) funciona igual.

**Si Hetzner rechaza la cuenta** (a veces piden verificación extra según el
país): Vultr tiene Ciudad de México y Miami, más cerca pero más caro (~20 $/mes
por 4 GB); Contabo tiene Nueva York y es más barato, con soporte y rendimiento
irregulares; DigitalOcean en Nueva York, ~24 $/mes.

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

**El dominio de la app y el de las identidades son independientes.** La app vive
en el dominio nuevo; el login usa las cuentas de Google Workspace del dominio
corporativo. No hay que tocar el DNS del corporativo, que es justamente el que
aloja el correo.

1. Añadir **el dominio nuevo** a Cloudflare (cambia sus servidores de nombres).
2. **Zero Trust → Networks → Tunnels**: crear un túnel, copiar su token a
   `TUNNEL_TOKEN`, y publicar la ruta `casambi.<dominio-nuevo>` →
   `http://casambi:8000`.
3. **Zero Trust → Settings → Authentication → Add new → Google**: pide un Client
   ID y un Client Secret, que se crean en Google Cloud Console → *APIs &
   Services* → *Credentials* → OAuth client ID, tipo *Web application*, con la
   URL de callback que muestra Cloudflare.
4. **Zero Trust → Access → Applications → Add an application → Self-hosted**,
   para `casambi.<dominio-nuevo>`. Política: *Include* → *Emails ending in* →
   `@<dominio-corporativo>`.
5. Copiar el **Application Audience (AUD) tag** a `CASAMBI_ACCESS_AUD`, y el
   dominio del equipo (`<equipo>.cloudflareaccess.com`) a
   `CASAMBI_ACCESS_TEAM_DOMAIN`.

#### Restringir a un subconjunto del dominio

La política por dominio da acceso a cualquiera con cuenta corporativa. Mientras
en Workspace estén las mismas personas que usan la herramienta, eso no concede
nada de más. Cuando deje de ser cierto —y conviene revisarlo, porque la app
enciende luces en instalaciones de clientes—, hay dos caminos:

- **Rápido**: añadir los correos a `CASAMBI_ACCESS_EMAILS` en `.env`. La app
  comprueba esa lista además de la firma del JWT, así que funciona sin tocar
  Cloudflare. Es también una segunda barrera por si la política de Access se
  relaja por error.
- **Ordenado**: un grupo de Workspace (`casambi@<dominio-corporativo>`) y una
  política de Access sobre ese grupo, de forma que dar y quitar acceso se haga
  en Google Workspace. **Ojo: los grupos exigen el proveedor «Google Workspace»,
  no el «Google» del paso 3**, y ese requiere además una cuenta de servicio en
  Google Cloud con delegación en todo el dominio y el permiso
  `admin.directory.group.readonly`. Elegir «Google» y esperar que el filtro por
  grupo funcione es un error silencioso: la política no encaja con nadie.

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
