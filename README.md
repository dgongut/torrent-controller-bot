# Torrent-Controller-Bot

**VERSIÓN EN ESPAÑOL** | [ENGLISH VERSION](README.en.md)

[![](https://badgen.net/badge/icon/github?icon=github&label)](https://github.com/dgongut/torrent-controller-bot)
[![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/torrent-controller-bot)
[![Docker Pulls](https://badgen.net/docker/pulls/dgongut/torrent-controller-bot?icon=docker&label=pulls)](https://hub.docker.com/r/dgongut/torrent-controller-bot/)
[![Docker Stars](https://badgen.net/docker/stars/dgongut/torrent-controller-bot?icon=docker&label=stars)](https://hub.docker.com/r/dgongut/torrent-controller-bot/)
[![Docker Image Size](https://badgen.net/docker/size/dgongut/torrent-controller-bot?icon=docker&label=image%20size)](https://hub.docker.com/r/dgongut/torrent-controller-bot/)
![Github stars](https://badgen.net/github/stars/dgongut/torrent-controller-bot?icon=github&label=stars)
![Github forks](https://badgen.net/github/forks/dgongut/torrent-controller-bot?icon=github&label=forks)
![Github last-commit](https://img.shields.io/github/last-commit/dgongut/torrent-controller-bot)
![Github last-commit](https://badgen.net/github/license/dgongut/torrent-controller-bot)

<img src="https://raw.githubusercontent.com/dgongut/pictures/main/torrent-controller-bot/torrent-controller-bot.png" width="150">

Lleva el control de tu gestor de torrents desde un único lugar.

- ✅ Panel de control con estado general: velocidades, espacio libre y torrents agrupados por estado
- ✅ Listado de torrents con filtros (descargando, compartiendo, pausados, completados, con error...) y paginación
- ✅ Búsqueda de torrents por nombre o por ruta
- ✅ Añadir torrents enviando un fichero `.torrent`, un enlace magnet o una URL a un fichero `.torrent`, eligiendo el directorio de descarga
- ✅ Pausar, reanudar, verificar, reanunciar al tracker y borrar torrents (con o sin sus datos)
- ✅ Mover torrents de directorio con aviso al terminar el movimiento
- ✅ Límites por torrent: hasta qué ratio compartir cada uno (el del gestor, sin límite, 0.5, 1, 2 u otro valor) y su velocidad de bajada y subida, y el ratio del propio gestor desde los ajustes
- ✅ Límites por tracker: ratio y velocidad para todos los torrents de un tracker, los que ya tiene y los que vayan llegando (por ejemplo, compartir sin fin en los privados y poco en los públicos), desde los ajustes o desde el filtro de ese tracker
- ✅ Renombrado con sugerencia automática para películas y series (`The.Matrix.1999.1080p.mkv` → `The Matrix (1999) - 1080p.mkv`)
- ✅ Renombrado de las carpetas y archivos que hay dentro de un torrent, uno a uno o en lote, arrastrando los subtítulos
- ✅ Series completas: renombra de una vez la carpeta, las subcarpetas de temporada y los episodios
- ✅ Acciones masivas sobre un filtro o búsqueda: reanudar, pausar, borrar, mover todos o fijarles el límite de ratio y de velocidad
- ✅ Filtro de torrents por tracker e información del tracker en el detalle de cada torrent
- ✅ Categorías de qBittorrent: filtrar por ellas, asignarlas y elegirlas al añadir un torrent
- ✅ Ajustes del gestor: modo tortuga y límites de velocidad de subida/bajada
- ✅ Notificaciones de descarga completada, de errores en torrents, de torrents añadidos fuera del bot y de torrents que dejan de compartir al llegar a su límite de ratio, con un botón para que sigan compartiendo (activables desde los ajustes)
- ✅ Descarga automática sin preguntar la ruta, con directorio configurable
- ✅ Renombrado automático al añadir un torrent, opcionalmente también de las subcarpetas y archivos que contiene y de los torrents añadidos fuera del bot (activable desde los ajustes)
- ✅ Sustituir una versión anterior: si el nombre que le toca a una descarga ya lo tiene otro torrent, al terminar te ofrece borrar el anterior (con sus datos) y quedarte con el nuevo, o lo hace sin preguntar si lo activas
- ✅ Carpeta de la serie: un episodio nuevo se ofrece (o va directo, si lo activas) a la carpeta donde ya tienes los anteriores
- ✅ Aviso si el torrent que añades puede no caber en el disco
- ✅ Ajustes del bot persistentes entre reinicios (volumen `/config`)
- ✅ Notificación al administrador al arrancar el bot
- ✅ Diseñado para bibliotecas grandes (probado con más de 10.000 torrents)
- ✅ Imagen multiarquitectura (amd64, arm64, armv7…) compatible con Raspberry Pi, NAS y servidores estándar
- ✅ Soporte de idiomas (Spanish, English)

Actualmente soporta **Transmission**, **qBittorrent**, **Deluge** y **Download Station de Synology** como gestores de torrents. La arquitectura interna es agnóstica al cliente, por lo que en el futuro podrán añadirse otros gestores. No todos ofrecen las mismas operaciones en su API: tienes el detalle en [qué puede hacer el bot con cada gestor](#qué-puede-hacer-el-bot-con-cada-gestor).

¿Lo buscas en [![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/torrent-controller-bot)?

## Crear tu bot de Telegram

Antes de levantar el contenedor necesitas un bot propio en Telegram y conocer tu identificador de usuario.

1. Abre [@BotFather](https://t.me/BotFather) en Telegram y envía `/newbot`. Sigue las instrucciones (un nombre y un username acabado en `bot`).

> [!WARNING]
> No le pongas al bot el nombre **TCB** ni un username que lo contenga: Telegram lo asocia con términos relacionados con drogas y puede banear el bot automáticamente. Usa un nombre más descriptivo, como por ejemplo `TorrentControllerBot`.

2. BotFather te devolverá el token del bot. Guárdalo: irá en la variable `TELEGRAM_TOKEN`.
3. Para conocer tu propio chat ID (lo necesitas para `TELEGRAM_ADMIN`), habla con [@MissRose_bot](https://t.me/MissRose_bot) y envíale `/id`. Te responderá con un número, ese es tu ID.
4. *(Opcional)* Si vas a usar el bot dentro de un grupo, añádelo, hazlo administrador y obtén el chat ID del grupo de la misma forma; ese valor irá en `TELEGRAM_GROUP`.
5. *(Opcional)* Si quieres ponerle el icono oficial al bot, descarga la imagen en alta resolución [aquí](https://raw.githubusercontent.com/dgongut/pictures/main/torrent-controller-bot/torrent-controller-bot.png) y envíasela a [@BotFather](https://t.me/BotFather) usando la opción `/setuserpic`.

## Comandos disponibles

| Comando | Descripción |
|---|---|
| `/start` | Panel de control con el estado general y los torrents agrupados por estado |
| `/list` | Listado completo de torrents con filtros y paginación |
| `/find texto` | Busca torrents por nombre o por ruta, sin importar mayúsculas ni tildes. Cada palabra puede ir en cualquier orden (`/find casa papel` encuentra `La.Casa.de.Papel`); una ruta tiene que aparecer tal cual (`/find /ssd/peliculas` no encuentra `/media/Peliculas`) y lo que va entre comillas, como frase exacta (`/find "la casa"`) |
| `/add` | Añade un torrent (también puedes enviar directamente un fichero `.torrent` o un enlace magnet) |
| `/settings` | Ajustes del gestor: modo tortuga, límites de velocidad y ratio |
| `/version` | Muestra la versión actual y el gestor al que está conectado |
| `/help` | Lista de comandos |
| `/donate` `/donors` | Dona al desarrollador / lista de donantes |

## Renombrado inteligente

Al pulsar ✏️ Renombrar sobre un torrent, el bot intenta generar una sugerencia de nombre limpio:

- **Películas**: `The.Matrix.1999.1080p.BluRay.x264.mkv` → `The Matrix (1999) - 1080p.mkv`
- **Series**: `Operaciones Especiales Lioness [HDTV 1080p][Cap.103]` → `1x03 - Operaciones Especiales Lioness - 1080p` (soporta formatos `Cap.103`, `S01E03`, `T01E03`, `3x05` y `Temporada 1 Capitulo 3`, rangos como `Cap.101_107` o `S01E01E02`, y packs como `2ª Temporada` o `Temporada Completa 1-5`)
- **Sin año**: si una película no lo trae, la sugerencia sale sin él (`El Hoyo 2 [WEB-DL 1080p]` → `El Hoyo 2 - 1080p`). Las plantillas personalizadas no cambian: si la tuya exige `{año}`, sigue sin sugerir nada

Si no puede generar sugerencia, siempre puedes escribir el nombre manualmente.

### Series completas

En un torrent que es una carpeta, 🪄 Renombrar lo renombra todo de una vez, con previsualización antes de confirmar: la carpeta principal, las subcarpetas de temporada y los episodios.

```
Mad.Men.2007.COMPLETE.1080p.NF.WEB-DL/                  Mad Men (2007) - 1080p/
├── Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2/    →    ├── T1 - Mad Men - 1080p/
│   └── Mad.Men.S01E01.1080p.mkv                        │   └── 1x01 - Mad Men - 1080p.mkv
└── Extras/                                             └── Extras/
```

Las subcarpetas usan la plantilla de temporada (o la de series o películas si la carpeta es un episodio o una película con su año) y toman del torrent lo que les falta, así que `Temporada 2` o `S04` también se renombran. Las carpetas que no identifican nada, como `Extras` o `Subs`, se quedan como están.

### Archivos dentro del torrent

Desde el detalle de un torrent, el botón 🗂️ Archivos permite navegar por sus carpetas (entrar en una subcarpeta, ⬆️ subir a la anterior) y renombrar la carpeta en la que estás, cada archivo uno a uno o todo lo que hay en la carpeta de golpe (con previsualización antes de confirmar). Los archivos heredan de su carpeta de temporada y del nombre del torrent lo que no llevan encima (título, año y temporada, nunca el episodio), así que un episodio suelto como `01.mkv` dentro de `Show.Name.S02.1080p` se convierte en `2x01 - Show Name - 1080p.mkv`. Los subtítulos que acompañan a un vídeo se renombran con él para no romper el emparejamiento, y todo se hace a través del gestor de torrents, por lo que el seeding se mantiene.

### Título del episodio

El campo `{titulo_episodio}` recoge el texto que va después del marcador de episodio, así que `Family.Guy.S01E01.Death.Has.a.Shadow.1080p.mkv` conserva `Death Has a Shadow`. No todos los episodios lo traen, por lo que conviene usarlo siempre entre corchetes:

```
{temporada}x{episodio.2} - {titulo}[ - {titulo_episodio}][.{extension}]
```

### Relleno con ceros

`{temporada}` y `{episodio}` admiten un sufijo `.N` que rellena el número con ceros hasta **N dígitos en total**, para que las temporadas y los episodios queden alineados:

| Plantilla | Temporada 1, episodio 7 |
|---|---|
| `{temporada}x{episodio}` | `1x7` |
| `{temporada}x{episodio.2}` | `1x07` |
| `{temporada.2}x{episodio.2}` | `01x07` |
| `{temporada.2}x{episodio.3}` | `01x007` |

Detalles a tener en cuenta:

- El número **nunca se recorta**: con `{episodio.2}`, el episodio 123 sigue siendo `123`.
- En rangos se rellena cada lado por separado, así que `S01-S08` con `{temporada.2}` da `01-08`.
- Solo funciona en `{temporada}` y `{episodio}`; en cualquier otro campo el bot rechaza la plantilla.
- `{capitulo}` no se ve afectado: mantiene siempre su formato de dos dígitos (`1x03`, `T2`).

## Sustituir una versión anterior

Si descargas algo que ya tenías (una copia mejor de la misma película, por ejemplo), el renombrado automático no lo renombra porque su nombre ya lo tiene el torrent antiguo, y te lo avisa al añadirlo. Cuando la descarga termina, el aviso de completado trae el botón 🔁 **Sustituir el anterior**: tras confirmarlo, el bot borra el torrent antiguo **con sus datos** y renombra el nuevo al nombre que le tocaba (y sus subcarpetas y archivos, si el renombrado automático los incluye).

```
✅ Descarga completada
🧲 Weapons (2025) 1080p NF WEB-DL DD+5.1 AV1-HDZ.mkv
♻️ Ya hay un torrent llamado Weapons (2025) - 1080p.mkv
      [🔁 Sustituir el anterior]  [❌ No sustituir]
```

❌ **No sustituir** deja los dos torrents como están y el aviso como un aviso de completado normal, sin botones.

Con **«Sustituir sin preguntar si el nombre ya existe»** (dentro del renombrado automático, desactivado por defecto) lo hace solo al terminar la descarga y te cuenta lo que ha borrado. Nunca se sustituye un torrent que comparte ficheros con el nuevo, porque borrar sus datos borraría también la descarga nueva.

## Carpeta de la serie

Cuando llega un episodio o un pack de temporada, el bot busca entre los torrents del gestor otros de la misma serie y te propone su carpeta. Para decidir que son la misma serie compara el título sin tener en cuenta mayúsculas, tildes ni puntuación (`Chicago.Fire.S14E04`, `14x03 - Chicago Fire.mkv` y `chicago fire s14e02` son la misma serie), y si los dos nombres llevan año, tiene que coincidir. Nunca hace coincidencias parciales: `Chicago Fire` y `Chicago Med` son series distintas, y una película como *El Camino: una película de Breaking Bad* no es un episodio de nada, así que no se le busca carpeta.

- **Al elegir la carpeta** (al añadir o al mover un torrent), la carpeta de la serie aparece la primera, marcada con ✨, aunque tengas muchas favoritas.
- **Con la descarga automática**, el mensaje de torrent añadido termina con un 💡 que propone la carpeta y los botones para moverlo o descartarlo. Al moverlo, el bot edita ese mismo mensaje para que muestre la carpeta nueva.
- **Con «Series directas a su carpeta»** (dentro de la descarga automática, desactivado por defecto), si no hay ninguna duda el torrent se añade directamente en la carpeta de la serie en vez de en la automática, y el mensaje lleva un botón ↩️ Deshacer. Solo ocurre cuando hay una única carpeta con el nombre de la serie; si hay dudas (dos carpetas posibles, o una carpeta común como `/media/series`), te la propone en lugar de decidir por ti. Opcionalmente se aplica también a los torrents añadidos fuera del bot, que en ese caso sí se mueven, porque ya están en el gestor.

Si guardas las temporadas en subcarpetas (`Mad Men/Season 01`), un episodio de la temporada siguiente se propone en `Mad Men/Season 02` con el mismo formato, aunque esa carpeta aún no exista. La carpeta de descargas por defecto y la de la descarga automática nunca se proponen, salvo que lleven el nombre de la serie. Un torrent de qBittorrent en gestión automática no se toca: la carpeta la decide su categoría.

## Qué puede hacer el bot con cada gestor

La arquitectura del bot es agnóstica al gestor, pero sus APIs no ofrecen las mismas operaciones. El bot **oculta los botones** de lo que el gestor configurado no sabe hacer, así que no verás opciones que fueran a fallar.

| | Transmission | qBittorrent | Deluge | Download Station |
|:---|:---:|:---:|:---:|:---:|
| Añadir por magnet o `.torrent` | ✅ | ✅ | ✅ | ✅ |
| Pausar y reanudar | ✅ | ✅ | ✅ | ✅ |
| Listados, búsqueda y filtros | ✅ | ✅ | ✅ | ✅ |
| Filtrar por tracker | ✅ | ✅ | ✅ | ✅ |
| Acciones masivas | ✅ | ✅ | ✅ | ✅ |
| Notificaciones de completado, error y añadidos externos | ✅ | ✅ | ✅ | ✅ |
| Mover los datos a otra carpeta | ✅ | ✅ | ✅ | ✅ |
| Borrar el torrent con sus datos | ✅ | ✅ | ✅ | ✅ |
| Espacio libre y aviso de disco | ✅ | ✅ | ✅ | ✅ |
| Límites de velocidad | ✅ | ✅ | ✅ | ✅ |
| Velocidad alternativa (modo tortuga) | ✅ | ✅ | ❌ ¹ | ❌ ¹ |
| Límite de ratio, por torrent y del gestor | ✅ | ✅ | ✅ ⁵ | ❌ ⁶ |
| Límite de velocidad por torrent | ✅ | ✅ | ✅ | ❌ ⁶ |
| Límites por tracker | ✅ | ✅ ⁸ | ✅ ⁸ | ❌ ⁶ |
| Reanunciar al tracker | ✅ | ✅ | ✅ | ❌ ⁷ |
| Verificar los datos | ✅ | ✅ | ✅ | ❌ ² |
| Renombrado inteligente, manual y automático | ✅ | ✅ | ✅ | ❌ ³ |
| Categorías | ❌ ⁴ | ✅ | ❌ ⁴ | ❌ ⁴ |

¹ Ni Deluge ni Download Station tienen modo de velocidad alternativa. Los límites de velocidad normales sí funcionan en ambos.

² Download Station no expone ninguna forma de volver a verificar los datos de una tarea.

⁴ Las categorías son un concepto de qBittorrent: cada una lleva asociada una carpeta, y un torrent en **gestión automática** (*Automatic Torrent Management*) saca su carpeta de su categoría, de modo que cambiársela mueve los ficheros. Por eso el bot no ofrece las dos cosas a la vez: en un torrent auto-gestionado el botón de mover se sustituye por el de categoría, y mover a mano queda disponible dentro de esa pantalla avisando de que el torrent pasa a modo manual. Transmission y Deluge tienen etiquetas, pero son etiquetas sueltas sin carpeta asociada (en Deluge hace falta además el plugin Label), así que el bot no las usa.

³ La API de Download Station no tiene ningún campo para el nombre de una tarea ni de los ficheros que contiene: lo único editable de una tarea es su destino. Se puede renombrar en disco con File Station, pero entonces la tarea deja de encontrar sus ficheros y se rompe en la siguiente verificación de DSM (un reinicio del NAS, del paquete, o una pausa y reanudación), así que el bot no lo hace. Es la misma limitación que tiene la propia interfaz de DSM, donde tampoco se puede renombrar una descarga.

⁵ Deluge no tiene torrents que «sigan» el ajuste global: lo copia en cada torrent al añadirlo. Elegir «El del gestor» vuelve a copiar el ajuste global actual en ese torrent, y cambiar el ratio del gestor desde los ajustes solo afecta a los torrents que se añadan después.

⁶ Download Station solo tiene ratio y límites de velocidad globales en sus ajustes; su API no permite cambiarlos para una tarea concreta.

⁷ La API de Download Station no tiene ninguna forma de pedir pares al tracker a demanda.

⁸ En su lista de torrents, qBittorrent y Deluge solo dicen el tracker con el que está trabajando cada uno (Deluge, además, por su dominio: `tracker.ejemplo.org` sale como `ejemplo.org`). Un torrent recién añadido recibe los límites de su tracker en cuanto lo anuncia por primera vez, y uno con varios trackers, según el que esté usando en ese momento.

## Download Station de Synology

Para usar Download Station (`TORRENT_CLIENT=download_station`) el bot se conecta a la API web de DSM con un usuario del NAS, el mismo tipo de cuenta con la que entras a DSM. Lo recomendable es **crear un usuario dedicado al bot**:

1. En DSM, ve a `Panel de control` → `Usuario y grupo` → `Crear`.
2. Ponle una contraseña y **no le actives la verificación en dos pasos**: la API de login la rechaza sin código TOTP y el bot no podrá arrancar.
3. En los permisos de aplicaciones, dale acceso a **Download Station** y a **File Station**. File Station se usa para mover datos, borrarlos, crear carpetas de destino que aún no existen y consultar el espacio libre, cosas que la API de Download Station no sabe hacer.
4. No hace falta que sea administrador ni que tenga acceso de escritura a más carpetas compartidas que las de descarga.

Esos datos van en `TORRENT_CLIENT_USER` y `TORRENT_CLIENT_PASSWORD`. El puerto es el de DSM: 5000 en http o 5001 en https (si usas 5001 pon `TORRENT_CLIENT_PROTOCOL=https`).

> [!NOTE]
> Las rutas de descarga de Download Station son relativas a la carpeta compartida, no absolutas: se escriben `Downloads` o `video/pelis`, no `/volume1/Downloads`. El bot crea la carpeta que le escribas si aún no existe, salvo que sea una carpeta compartida, que solo puede crearse desde DSM.

> [!NOTE]
> El bot lista **todas** las tareas de Download Station, también las descargas HTTP o FTP que hayas añadido desde DSM. Con esas, Download Station considera la descarga cerrada en cuanto termina y ya no permite cambiarle el destino: si intentas moverla, el bot te lo dice y no toca los ficheros.

> [!NOTE]
> Mover un torrent que está compartiendo lo deja unos segundos en espera y con una verificación rápida antes de volver a compartir. Es el comportamiento normal de DSM al cambiarle el destino, no un error.

## Configuración en las variables del Docker Compose

| CLAVE  | OBLIGATORIO | VALOR |
|:------------- |:---------------:| :-------------|
|TELEGRAM_TOKEN |✅| Token del bot |
|TELEGRAM_ADMIN |✅| ChatId del administrador (se puede obtener hablándole al bot [Rose](https://t.me/MissRose_bot) escribiendo /id). Admite múltiples administradores separados por comas. Por ejemplo 12345,54431,55944 |
|TELEGRAM_GROUP |❌| ChatId del grupo. Si este bot va a formar parte de un grupo, es necesario especificar el chatId de dicho grupo. Es necesario que el bot sea administrador del grupo |
|TELEGRAM_THREAD |❌| Thread del tema dentro de un supergrupo; valor numérico (2,3,4..). Por defecto 1. Se utiliza en conjunción con la variable TELEGRAM_GROUP |
|TZ |✅| Timezone (Por ejemplo Europe/Madrid) |
|LANGUAGE |❌| Idioma, puede ser ES / EN. Por defecto ES (Spanish) |
|TORRENT_CLIENT |❌| Gestor de torrents: `transmission`, `qbittorrent`, `deluge` o `download_station`. Por defecto transmission |
|TORRENT_CLIENT_HOST |✅| Host o IP donde está el gestor de torrents |
|TORRENT_CLIENT_PORT |❌| Puerto del gestor de torrents. Por defecto 9091 (Transmission), 8080 (qBittorrent), 8112 (Deluge Web UI) o 5000/5001 (DSM de Synology) |
|TORRENT_CLIENT_USER |❌| Usuario del gestor de torrents, si tiene autenticación |
|TORRENT_CLIENT_PASSWORD |❌| Contraseña del gestor de torrents, si tiene autenticación |
|TORRENT_CLIENT_PROTOCOL |❌| Protocolo de conexión, http o https. Por defecto http |
|TORRENT_CLIENT_RPC_PATH |❌| Ruta del RPC. Por defecto /transmission/rpc (Transmission) o /webapi (Synology) |
|TORRENTS_PER_PAGE |❌| Número de torrents por página en los listados. Por defecto 10 |
|DASHBOARD_REFRESH_SECONDS |❌| Segundos entre refrescos automáticos del panel de control, las listas y la ficha de un torrent. Por defecto 2 |
|DASHBOARD_REFRESH_DURATION |❌| Segundos que dura el refresco automático del panel de control, las listas y la ficha de un torrent. Por defecto 60 |
|TELEMETRY |❌| `false` para desactivar las [estadísticas anónimas](#estadísticas-anónimas) sin pasar por `/settings`. Lo normal es desactivarlas desde `/settings`; esto es para quien prefiera dejarlo fijado en el compose |

## Ejemplo de Docker-Compose para su ejecución normal

```yaml
services:
    torrent-controller-bot:
        environment:
            - TELEGRAM_TOKEN=
            - TELEGRAM_ADMIN=
            - TZ=Europe/Madrid
            - TORRENT_CLIENT=transmission
            - TORRENT_CLIENT_HOST=
            #- TELEGRAM_GROUP=
            #- TELEGRAM_THREAD=1
            #- LANGUAGE=ES
            #- TORRENT_CLIENT_PORT=9091
            #- TORRENT_CLIENT_USER=
            #- TORRENT_CLIENT_PASSWORD=
        image: dgongut/torrent-controller-bot:latest
        container_name: torrent-controller-bot
        restart: always
        tty: true
        volumes:
            - /ruta/de/tu/eleccion:/config # Ajustes persistentes del bot
```

## Estadísticas anónimas

Desde la 1.7.0 el bot envía una vez al día unas cifras anónimas para saber cuánta gente lo usa, con qué gestor de torrents y qué funciones se usan más. Así sé dónde poner el esfuerzo. Las estadísticas son públicas: [stats.dgongut.com/torrent-controller-bot](https://stats.dgongut.com/torrent-controller-bot).

**Qué se envía:** el gestor y su versión principal, cuántos torrents gestiona, qué ajustes están activados, la versión del bot, la arquitectura, cuántas veces al día se ha usado cada comando y cada botón, y cómo le llegan los torrents (fichero, magnet o URL). La lista completa, campo a campo, está en [la página de privacidad](https://stats.dgongut.com/torrent-controller-bot/privacy). El servidor descarta cualquier dato que no esté en ella. Desde `/settings` → *Estadísticas anónimas* → *Ver qué se envía* puedes ver exactamente lo que saldría en el siguiente envío.

**Qué no se envía nunca:** nombres de torrents, rutas, categorías, trackers, direcciones del gestor, IDs de Telegram ni nada de lo que escribes. Tu IP no se guarda.

**Cómo se desactiva:** desde `/settings` → *Estadísticas anónimas*, o con `TELEMETRY=false`. Al desactivarlas se borra también el identificador de la instalación.

Están activadas por defecto. Nada se envía hasta que el bot lleva al menos 10 minutos en marcha. Tampoco se envía si `/config` no está en un volumen, porque sin él cada vez que se recreara el contenedor contaría como una instalación nueva.

## Anotaciones
> [!NOTE]
> El bot no necesita acceso a los ficheros descargados: se comunica con el gestor de torrents únicamente por RPC. El único volumen que hay que mapear es `/config`, donde el bot guarda sus ajustes para que persistan entre reinicios.

> [!NOTE]
> Si Transmission corre en la misma máquina que el bot pero fuera de Docker, usa `host.docker.internal` como `TORRENT_CLIENT_HOST` (en Linux añade `extra_hosts: ["host.docker.internal:host-gateway"]`).

---
## Solo para desarrolladores

### Ejecución con código local

Para su ejecución en local y probar nuevos cambios de código, se necesita renombrar el fichero `.env.example` a `.env` con los valores necesarios para su ejecución.
Es necesario establecer un `TELEGRAM_TOKEN` y un `TELEGRAM_ADMIN` correctos y diferentes al de la ejecución normal.

La estructura de carpetas debe quedar:

```
torrent-controller-bot/
    ├── .env
    ├── .gitignore
    ├── LICENSE
    ├── requirements.txt
    ├── README.md
    ├── bot_settings.py
    ├── config.py
    ├── logger.py
    ├── message_queue.py
    ├── name_parser.py
    ├── torrent-controller-bot.py
    ├── Dockerfile_local
    ├── docker-compose.yaml
    ├── torrent_clients
    │   ├── __init__.py
    │   ├── base.py
    │   ├── deluge_client.py
    │   ├── factory.py
    │   ├── qbittorrent_client.py
    │   ├── synology_client.py
    │   └── transmission_client.py
    └── locale
        ├── en.json
        └── es.json
```

Para levantarlo habría que ejecutar en esa ruta: `docker compose -f docker-compose.yaml up -d --build --force-recreate` utilizando el `Dockerfile_local`.

También puede ejecutarse directamente con Python:

```bash
pip install -r requirements.txt
set -a && source .env && set +a
LOCALE_PATH=./locale python3 torrent-controller-bot.py
```
