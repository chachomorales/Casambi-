# Roadmap

Lo pendiente y lo que espera un dato de fuera. Lo que se descartó también se
apunta, con el porqué, para que nadie lo retome sin saberlo.

## Pendiente

### Acceso a la web por grupo de Google Workspace

Abierto desde el 2026-09-27, con el gerente de IT. Hoy Cloudflare Access deja
pasar por correo; la idea es abrirlo a un grupo de impelsa.com (p. ej.
`casambi@impelsa.com`), para dar y quitar acceso desde Workspace sin tocar
Cloudflare ni el servidor.

1. **IT:** crear el grupo, registrar el redirect URI
   `https://impelsa.cloudflareaccess.com/cdn-cgi/access/callback`, activar la
   Admin SDK API y dar delegación de dominio con el scope
   `admin.directory.group.readonly`.
2. **Cloudflare:** login method **Google Workspace** (no «Google»); el botón
   Test debe listar los grupos.
3. **Política de la app:** Include → Google Workspace groups; quitar la regla
   vieja y el login «Google».
4. **Servidor:** revisar `CASAMBI_ACCESS_EMAILS` en `/opt/casambi/.env`. Si solo
   lleva un correo, la app rechaza al grupo aunque Cloudflare lo deje pasar.

Al terminar, actualizar `despliegue/README.md` (sección «Restringir a un
subconjunto del dominio»).

### Medición de energía del McWong PSC-WCM-450

En el Lab (2026-10-01), con el módulo en línea y carga puesta, `Voltage`,
`Current`, `Power` y `Energy` siguieron en 0 y con la hora quieta. Hay que
preguntar a la fábrica qué ajuste le falta para medir. El código ya los lee y
los muestra (`report.medida_electrica()`): no hay nada que tocar hasta tener la
respuesta.

## A la espera de un dato externo

- **Umbral de temperatura de las Eulum TRED-E-CSB-2A.** Se deja sin alarma a
  propósito: no sabemos a partir de qué temperatura sufre el driver, y acusar
  a equipos sanos es lo que la sección de Conectividad evita. Cuando llegue la
  ficha técnica, el umbral va en `report.temperatura_unidad()`.
- **Significado de `condition = 128`.** La API no lo documenta; hoy sale como
  aviso sin causa. Si se averigua, se traduce.

## Descartado

- **Recalcular en Python el mapa de calor del Simulador de Cobertura.** Habría
  que portar ~700 líneas de Swift (`Propagation`, `Raycast`, `WallIndex`,
  `Heatmap`, `CasambiProfile`) y mantener dos implementaciones de la misma
  física. El mapa se exporta desde el simulador (⇧⌘E) y se sube como imagen.
