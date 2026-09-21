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
| VPS | Hetzner Cloud, x86 con **4 GB de RAM como mínimo** | ~5-9 $/mes |
| Dominio, aparte del corporativo | cualquier registrador | ~12 €/año |
| Cuenta Cloudflare, Zero Trust Free | cloudflare.com | 0 € (hasta 50 usuarios) |
| Copias | Backblaze B2 | 0 € (10 GB gratis; aquí se usan menos de 10 MB) |
| Identidad | Google Workspace, ya contratado | 0 € |

**Sobre la ubicación.** El equipo y los clientes están en Guatemala. Desde allí,
Ashburn (Virginia) queda a unos 50-70 ms y Europa a 180-200 ms. Las ubicaciones
europeas de Hetzner son más baratas, así que es un intercambio legítimo: en
Europa la app se nota algo menos ágil, pero es perfectamente usable para consultar
informes, y Cloudflare absorbe parte del trayecto porque el tráfico entra por su
punto de presencia local y viaja por su troncal. **El despliegue actual es un
x86 de 4 GB en Núremberg** (`ubuntu-4gb-nbg1-1`: 2 vCPU, 3,7 GB de RAM, 38 GB de
disco) con Ubuntu 26.04 LTS. Migrar más adelante es recrear el servidor y
restaurar la copia.

Se intentó levantarlo en un CAX11 Arm, más barato a igual tamaño, y no se pudo:
la sub-pestaña **Arm64** del formulario de Hetzner no ofrecía ningún tipo en las
ubicaciones probadas. Es falta de existencias, y va y viene. **No merece la pena
reintentarlo**, y conviene dejarlo escrito para no gastar otra tarde en ello: la
arquitectura es transparente en este despliegue porque la imagen se construye en
el propio servidor, y Pillow, PyMuPDF y cryptography publican ruedas para x86-64
y para aarch64 por igual. Las tres imágenes de terceros —cloudflared, restic y
alpine— también traen las dos. La diferencia de precio entre un CAX y el CX
equivalente son céntimos al mes.

Los servidores Arm (CAX) son solo europeos; en EEUU se usa un x86 de la serie
CPX.

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
   | Location | Ashburn (más cerca) o Alemania/Finlandia (más barato). El actual está en Núremberg |
   | Image | Ubuntu LTS; el actual usa 26.04 (`resolute`) |
   | Type | x86, **con 4 GB de RAM como mínimo** |
   | Networking | dejar la IPv4 pública (para el SSH) |
   | SSH keys | **marcar la casilla de la clave en este formulario**; nunca contraseña |
   | Firewalls | uno que solo permita el 22. Con el túnel no hace falta abrir más |

**Marcar la clave SSH es el paso que más caro sale olvidar.** Tenerla registrada
en la cuenta de Hetzner no basta: si la casilla se queda sin marcar, el servidor
nace con `authorized_keys` vacío, Hetzner manda una contraseña de root por correo
y solo queda entrar por la consola web. Y esa consola es noVNC, que **teclea mal
varios caracteres con teclado español**: el guion bajo sale como guion, `&&` como
`77`, `>>` como `..` y `:` como `;`. Escribir ahí una clave SSH es inviable —
`authorized_keys` acaba llamándose `authorized-keys` y el `echo` imprime en
pantalla en vez de redirigir. Si se llega a esa situación, la salida no es pelear
con el teclado: `passwd` en la consola, y `ssh-copy-id` desde el Mac.

**Los 4 GB son lo que no conviene recortar.** `cobertura.parse_proyecto` mantiene
descomprimidos a la vez todos los planos de un proyecto multinivel; el tope de 20
niveles (`config.MAX_NIVELES_COBERTURA`) acota el peor caso, pero el margen con
2 GB es escaso y la diferencia de precio son un par de dólares.

Las dependencias nativas —Pillow, PyMuPDF, cryptography— traen ruedas manylinux
para x86-64 y para aarch64, así que la arquitectura no condiciona nada: el día que
haya existencias de CAX (Arm), o que se migre a Ashburn con un CPX, funciona igual.

**Si Hetzner rechaza la cuenta** (a veces piden verificación extra según el
país): Vultr tiene Ciudad de México y Miami, más cerca pero más caro (~20 $/mes
por 4 GB); Contabo tiene Nueva York y es más barato, con soporte y rendimiento
irregulares; DigitalOcean en Nueva York, ~24 $/mes.

## Puesta en marcha

### 1. El servidor

Lo primero, cerrar el SSH. Ubuntu deja `PasswordAuthentication yes` puesto en
`/etc/ssh/sshd_config.d/50-cloud-init.conf`:

```sh
cat > /etc/ssh/sshd_config.d/01-casambi.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t && systemctl reload ssh
sshd -T | grep -E '^(passwordauthentication|permitrootlogin)'
```

**El `01` del nombre no es decorativo.** sshd se queda con el **primer** valor que
encuentra de cada opción y lee `sshd_config.d` en orden alfabético, así que un
`99-casambi.conf` iría después del `50-cloud-init.conf` y no serviría de nada.
El `sshd -T` del final es lo que confirma el valor efectivo, no el escrito.

Después, Docker desde el repositorio oficial:

```sh
apt-get update && apt-get install -y ca-certificates curl gnupg git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
docker compose version
```

Se usa el repositorio de Docker en vez del `docker.io` de Ubuntu porque da
versiones al día y quita la duda de si el plugin v2 está empaquetado. Con una
imagen de Ubuntu recién salida, conviene comprobar antes que Docker publica para
su nombre en clave:

```sh
. /etc/os-release
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://download.docker.com/linux/ubuntu/dists/$VERSION_CODENAME/Release"
```

Un 200 y adelante. Para 26.04 (`resolute`) responde 200.

**Sobre correr como root.** El despliegue vive en `/opt/casambi` y lo maneja root.
Un usuario aparte sería más ortodoxo, pero tendría que estar en el grupo `docker`,
y pertenecer a ese grupo **equivale a ser root en el host**: basta con montar `/`
dentro de un contenedor. La separación sería cosmética. Lo que sí importa, y sí
está hecho, es que la app no corre como root *dentro* del contenedor: el
Dockerfile crea el usuario `casambi` (uid 1000) y gunicorn arranca con él.

### 2. El código y la configuración

```sh
git clone git@github.com:chachomorales/Casambi-.git /opt/casambi
cd /opt/casambi
cp despliegue/.env.deploy.example .env && chmod 600 .env
```

El clon va sobre `main`, que es la rama única desde el 2026-09-20. Un servidor
de antes de esa fecha puede seguir en la rama `web`, ya borrada: se pasa con
`git -C /opt/casambi checkout main` una sola vez, y a partir de ahí `git pull`
vuelve a traer cambios.

El repositorio es privado, así que el servidor necesita clave propia. Se genera
en el servidor y la pública se añade **como deploy key del repositorio**, sin
permiso de escritura (GitHub → el repo → Settings → Deploy keys):

```sh
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519_github -N '' -C 'casambi-server-deploy'
printf 'Host github.com\n  IdentityFile /root/.ssh/id_ed25519_github\n  IdentitiesOnly yes\n' \
  > /root/.ssh/config && chmod 600 /root/.ssh/config
```

**Deploy key del repositorio, no clave de la cuenta.** Una clave de cuenta da
acceso de lectura y escritura a *todos* los repositorios del usuario, y esto es
una máquina expuesta a internet. Se distinguen probando la conexión: `ssh -T
git@github.com` responde `Hi usuario/repo!` con una deploy key y `Hi usuario!`
con una de cuenta.

Rellenar `.env`. La clave se genera así:

```sh
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

> **Guarda `CASAMBI_SECRET_KEY` en el gestor de contraseñas de la empresa, fuera
> del servidor.** Sin ella no se pueden descifrar las credenciales de Casambi, ni
> las del servidor ni las de una copia de seguridad.

`CASAMBI_TZ` es opcional y vale `America/Guatemala` si no se pone: es la zona con
la que se fechan la bitácora y los informes. El reloj del servidor puede seguir
en UTC o en la hora de Alemania —la app no lo mira—, pero si algún día el equipo
trabaja desde otro país, esta es la variable que hay que cambiar.

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

**El nombre del equipo se elige una vez y hay que elegirlo pronto.** Al activar
Zero Trust, Cloudflare asigna uno al azar del estilo `autumn-frost-4839`, y ese
nombre es el dominio que verá el equipo en la pantalla de login. Cambiarlo es un
botón en *Settings → Team name*, pero **el dominio del equipo va dentro de la URL
de retorno registrada en Google**, así que cambiarlo después de crear el cliente
OAuth rompe el login con `redirect_uri_mismatch`. Renombrarlo antes de tocar
Google no cuesta nada; después, obliga a repasar Google y los proveedores.

El AUD no aparece en el JSON de configuración de la aplicación. Está en la ficha
de la aplicación (`···` → *Copy AUD*), y se distingue del *Policy ID* por la
forma: el AUD son 64 caracteres hexadecimales sin guiones; el Policy ID es un
UUID de 36 con guiones.

**El despliegue actual**: dominio `casambigt.com` —registrado en Wild West
Domains (GoDaddy) y con los nameservers apuntando a Cloudflare—, aplicación en
`casambi.casambigt.com`, equipo `impelsa.cloudflareaccess.com`, e identidad por
Google con la pantalla de consentimiento en modo **Interno**, que restringe el
login al Workspace de Impelsa y evita la verificación de Google.

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
# 0 3 * * *  /opt/casambi/despliegue/copia.sh >> /var/log/casambi-copia.log 2>&1
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
