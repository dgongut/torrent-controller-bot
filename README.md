# Torrent-Controller-Bot
[![](https://badgen.net/badge/icon/github?icon=github&label)](https://github.com/dgongut/torrent-controller-bot)
[![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/torrent-controller-bot)
[![Docker Pulls](https://badgen.net/docker/pulls/dgongut/torrent-controller-bot?icon=docker&label=pulls)](https://hub.docker.com/r/dgongut/torrent-controller-bot/)
[![Docker Stars](https://badgen.net/docker/stars/dgongut/torrent-controller-bot?icon=docker&label=stars)](https://hub.docker.com/r/dgongut/torrent-controller-bot/)
[![Docker Image Size](https://badgen.net/docker/size/dgongut/torrent-controller-bot?icon=docker&label=image%20size)](https://hub.docker.com/r/dgongut/torrent-controller-bot/)
![Github stars](https://badgen.net/github/stars/dgongut/torrent-controller-bot?icon=github&label=stars)
![Github forks](https://badgen.net/github/forks/dgongut/torrent-controller-bot?icon=github&label=forks)
![Github last-commit](https://img.shields.io/github/last-commit/dgongut/torrent-controller-bot)
![Github last-commit](https://badgen.net/github/license/dgongut/torrent-controller-bot)

Lleva el control de tu gestor de torrents desde un único lugar.

- ✅ Panel de control con estado general: velocidades, espacio libre y torrents agrupados por estado
- ✅ Listado de torrents con filtros (descargando, compartiendo, pausados, completados, con error...) y paginación
- ✅ Búsqueda de torrents por nombre o por ruta
- ✅ Añadir torrents enviando un fichero `.torrent`, un enlace magnet o una URL a un fichero `.torrent`, eligiendo el directorio de descarga
- ✅ Pausar, reanudar, verificar y borrar torrents (con o sin sus datos)
- ✅ Mover torrents de directorio con aviso al terminar el movimiento
- ✅ Renombrado con sugerencia automática para películas y series (`The.Matrix.1999.1080p.mkv` → `The Matrix (1999) - 1080p.mkv`)
- ✅ Acciones masivas sobre un filtro o búsqueda: reanudar, pausar, borrar o mover todos
- ✅ Filtro de torrents por tracker e información del tracker en el detalle de cada torrent
- ✅ Ajustes del gestor: modo tortuga y límites de velocidad de subida/bajada
- ✅ Notificaciones de descarga completada y de errores en torrents (activables desde los ajustes)
- ✅ Descarga automática sin preguntar la ruta, con directorio configurable
- ✅ Renombrado automático al añadir un torrent (activable desde los ajustes)
- ✅ Aviso si el torrent que añades puede no caber en el disco
- ✅ Ajustes del bot persistentes entre reinicios (volumen `/config`)
- ✅ Notificación al administrador al arrancar el bot
- ✅ Diseñado para bibliotecas grandes (probado con más de 10.000 torrents)
- ✅ Imagen multiarquitectura (amd64, arm64, armv7…) compatible con Raspberry Pi, NAS y servidores estándar
- ✅ Soporte de idiomas (Spanish, English)

Actualmente soporta **Transmission**, **qBittorrent** y **Deluge** como gestores de torrents. La arquitectura interna es agnóstica al cliente, por lo que en el futuro podrán añadirse otros gestores.

¿Lo buscas en [![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/torrent-controller-bot)?

## Crear tu bot de Telegram

Antes de levantar el contenedor necesitas un bot propio en Telegram y conocer tu identificador de usuario.

1. Abre [@BotFather](https://t.me/BotFather) en Telegram y envía `/newbot`. Sigue las instrucciones (un nombre y un username acabado en `bot`).
2. BotFather te devolverá el token del bot. Guárdalo: irá en la variable `TELEGRAM_TOKEN`.
3. Para conocer tu propio chat ID (lo necesitas para `TELEGRAM_ADMIN`), habla con [@MissRose_bot](https://t.me/MissRose_bot) y envíale `/id`. Te responderá con un número, ese es tu ID.
4. *(Opcional)* Si vas a usar el bot dentro de un grupo, añádelo, hazlo administrador y obtén el chat ID del grupo de la misma forma; ese valor irá en `TELEGRAM_GROUP`.

## Comandos disponibles

| Comando | Descripción |
|---|---|
| `/start` | Panel de control con el estado general y los torrents agrupados por estado |
| `/list` | Listado completo de torrents con filtros y paginación |
| `/find texto` | Busca torrents por nombre o por ruta (p. ej. `/find /downloads`) |
| `/add` | Añade un torrent (también puedes enviar directamente un fichero `.torrent` o un enlace magnet) |
| `/settings` | Ajustes del gestor: modo tortuga y límites de velocidad |
| `/version` | Muestra la versión actual y el gestor al que está conectado |
| `/help` | Lista de comandos |

## Renombrado inteligente

Al pulsar ✏️ Renombrar sobre un torrent, el bot intenta generar una sugerencia de nombre limpio:

- **Películas**: `The.Matrix.1999.1080p.BluRay.x264.mkv` → `The Matrix (1999) - 1080p.mkv`
- **Series**: `Operaciones Especiales Lioness [HDTV 1080p][Cap.103]` → `1x03 - Operaciones Especiales Lioness - 1080p` (soporta formatos `Cap.103`, `S01E03` y `3x05`)

Si no puede generar sugerencia, siempre puedes escribir el nombre manualmente.

## Configuración en las variables del Docker Compose

| CLAVE  | OBLIGATORIO | VALOR |
|:------------- |:---------------:| :-------------|
|TELEGRAM_TOKEN |✅| Token del bot |
|TELEGRAM_ADMIN |✅| ChatId del administrador (se puede obtener hablándole al bot [Rose](https://t.me/MissRose_bot) escribiendo /id). Admite múltiples administradores separados por comas. Por ejemplo 12345,54431,55944 |
|TELEGRAM_GROUP |❌| ChatId del grupo. Si este bot va a formar parte de un grupo, es necesario especificar el chatId de dicho grupo. Es necesario que el bot sea administrador del grupo |
|TELEGRAM_THREAD |❌| Thread del tema dentro de un supergrupo; valor numérico (2,3,4..). Por defecto 1. Se utiliza en conjunción con la variable TELEGRAM_GROUP |
|TZ |✅| Timezone (Por ejemplo Europe/Madrid) |
|LANGUAGE |❌| Idioma, puede ser ES / EN. Por defecto ES (Spanish) |
|TORRENT_CLIENT |❌| Gestor de torrents: `transmission`, `qbittorrent` o `deluge`. Por defecto transmission |
|TORRENT_CLIENT_HOST |✅| Host o IP donde está el gestor de torrents |
|TORRENT_CLIENT_PORT |❌| Puerto del gestor de torrents. Por defecto 9091 (Transmission), 8080 (qBittorrent) u 8112 (Deluge Web UI) |
|TORRENT_CLIENT_USER |❌| Usuario del gestor de torrents, si tiene autenticación |
|TORRENT_CLIENT_PASSWORD |❌| Contraseña del gestor de torrents, si tiene autenticación |
|TORRENT_CLIENT_PROTOCOL |❌| Protocolo de conexión, http o https. Por defecto http |
|TORRENT_CLIENT_RPC_PATH |❌| Ruta del RPC. Por defecto /transmission/rpc (Transmission) |
|TORRENTS_PER_PAGE |❌| Número de torrents por página en los listados. Por defecto 10 |
|DASHBOARD_REFRESH_SECONDS |❌| Segundos entre refrescos automáticos del panel de control. Por defecto 2 |
|DASHBOARD_REFRESH_DURATION |❌| Segundos que dura el refresco automático del panel de control. Por defecto 60 |

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
    ├── smart_rename.py
    ├── torrent-controller-bot.py
    ├── Dockerfile_local
    ├── docker-compose.yaml
    ├── torrent_clients
    │   ├── __init__.py
    │   ├── base.py
    │   ├── deluge_client.py
    │   ├── factory.py
    │   ├── qbittorrent_client.py
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
