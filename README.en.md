# Torrent-Controller-Bot

[VERSIÓN EN ESPAÑOL](README.md) | **ENGLISH VERSION**

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

Control your torrent client from a single place.

- ✅ Dashboard with overall status: speeds, free space and torrents grouped by state
- ✅ Torrent list with filters (downloading, seeding, paused, completed, with errors...) and pagination
- ✅ Search torrents by name or by path
- ✅ Add torrents by sending a `.torrent` file, a magnet link or a URL to a `.torrent` file, choosing the download directory
- ✅ Pause, resume, verify and delete torrents (with or without their data)
- ✅ Move torrents to another directory with a notice when the move finishes
- ✅ Renaming with automatic suggestion for movies and series (`The.Matrix.1999.1080p.mkv` → `The Matrix (1999) - 1080p.mkv`)
- ✅ Renaming the files inside a torrent, one by one or in batch, dragging their subtitles along
- ✅ Mass actions over a filter or search: resume, pause, delete or move all
- ✅ Filter torrents by tracker and tracker info in each torrent's detail
- ✅ Client settings: turtle mode and upload/download speed limits
- ✅ Notifications for completed downloads and torrent errors (can be toggled from the settings)
- ✅ Automatic download without asking for the path, with a configurable directory
- ✅ Automatic rename when adding a torrent, optionally including the files it contains (can be toggled from the settings)
- ✅ Warning if the torrent you add may not fit on the disk
- ✅ Bot settings persist across restarts (`/config` volume)
- ✅ Notification to the administrator when the bot starts
- ✅ Designed for large libraries (tested with more than 10,000 torrents)
- ✅ Multi-arch image (amd64, arm64, armv7…) compatible with Raspberry Pi, NAS and standard servers
- ✅ Language support (Spanish, English)

It currently supports **Transmission**, **qBittorrent**, **Deluge** and **Synology Download Station** as torrent clients. The internal architecture is client-agnostic, so more clients may be added in the future. Their APIs do not all offer the same operations: the detail is in [what the bot can do with each client](#what-the-bot-can-do-with-each-client).

Looking for it on [![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/torrent-controller-bot)?

## Create your Telegram bot

Before starting the container you need your own Telegram bot and your user identifier.

1. Open [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`. Follow the instructions (a name and a username ending in `bot`).

> [!WARNING]
> Do NOT name the bot **TCB** or use a username containing it: Telegram associates it with drug-related terms and may ban the bot automatically. Use a more descriptive name instead, such as `TorrentControllerBot`.

2. BotFather will give you the bot token. Save it: it goes in the `TELEGRAM_TOKEN` variable.
3. To find out your own chat ID (needed for `TELEGRAM_ADMIN`), talk to [@MissRose_bot](https://t.me/MissRose_bot) and send `/id`. It will reply with a number, that is your ID.
4. *(Optional)* If you are going to use the bot inside a group, add it, make it an administrator and get the group chat ID the same way; that value goes in `TELEGRAM_GROUP`.
5. *(Optional)* If you want to set the official icon for the bot, download the high-resolution image [here](https://raw.githubusercontent.com/dgongut/pictures/main/torrent-controller-bot/torrent-controller-bot.png) and send it to [@BotFather](https://t.me/BotFather) using the `/setuserpic` option.

## Available commands

| Command | Description |
|---|---|
| `/start` | Dashboard with the overall status and torrents grouped by state |
| `/list` | Full torrent list with filters and pagination |
| `/find text` | Search torrents by name or by path (e.g. `/find /downloads`) |
| `/add` | Add a torrent (you can also send a `.torrent` file or a magnet link directly) |
| `/settings` | Client settings: turtle mode and speed limits |
| `/version` | Shows the current version and the client it is connected to |
| `/help` | List of commands |

## Smart renaming

When pressing ✏️ Rename on a torrent, the bot tries to generate a clean name suggestion:

- **Movies**: `The.Matrix.1999.1080p.BluRay.x264.mkv` → `The Matrix (1999) - 1080p.mkv`
- **Series**: `Operaciones Especiales Lioness [HDTV 1080p][Cap.103]` → `1x03 - Operaciones Especiales Lioness - 1080p` (supports `Cap.103`, `S01E03` and `3x05` formats)

If no suggestion can be generated, you can always type the name manually.

### Files inside the torrent

From a torrent's detail, the 🗂️ Files button lists the files it contains and lets you rename them one by one or all at once (with a preview before confirming). Files inherit from the torrent name whatever they do not carry themselves (title, year and season, never the episode), so a bare episode like `01.mkv` inside `Show.Name.S02.1080p` becomes `2x01 - Show Name - 1080p.mkv`. Subtitles next to a video are renamed along with it so they keep matching, and everything goes through the torrent client, so seeding is preserved.

### Episode title

The `{episode_title}` field picks up the text that follows the episode marker, so `Family.Guy.S01E01.Death.Has.a.Shadow.1080p.mkv` keeps `Death Has a Shadow`. Not every episode carries one, so it is best used inside brackets:

```
{season}x{episode.2} - {title}[ - {episode_title}][.{extension}]
```

### Zero-padding

`{season}` and `{episode}` accept a `.N` suffix that pads the number with zeros up to **N digits in total**, so seasons and episodes line up:

| Template | Season 1, episode 7 |
|---|---|
| `{season}x{episode}` | `1x7` |
| `{season}x{episode.2}` | `1x07` |
| `{season.2}x{episode.2}` | `01x07` |
| `{season.2}x{episode.3}` | `01x007` |

Things to keep in mind:

- The number is **never truncated**: with `{episode.2}`, episode 123 stays `123`.
- Ranges are padded on each side, so `S01-S08` with `{season.2}` gives `01-08`.
- It only works on `{season}` and `{episode}`; on any other field the bot rejects the template.
- `{chapter}` is unaffected: it always keeps its two-digit format (`1x03`, `T2`).

## What the bot can do with each client

The architecture of the bot is client-agnostic, but their APIs do not offer the same operations. The bot **hides the buttons** for whatever the configured client cannot do, so you never see options that would fail.

| | Transmission | qBittorrent | Deluge | Download Station |
|:---|:---:|:---:|:---:|:---:|
| Add by magnet or `.torrent` | ✅ | ✅ | ✅ | ✅ |
| Pause and resume | ✅ | ✅ | ✅ | ✅ |
| Listings, search and filters | ✅ | ✅ | ✅ | ✅ |
| Filter by tracker | ✅ | ✅ | ✅ | ✅ |
| Mass actions | ✅ | ✅ | ✅ | ✅ |
| Completed and error notifications | ✅ | ✅ | ✅ | ✅ |
| Move the data to another folder | ✅ | ✅ | ✅ | ✅ |
| Delete the torrent along with its data | ✅ | ✅ | ✅ | ✅ |
| Free space and low disk warning | ✅ | ✅ | ✅ | ✅ |
| Speed limits | ✅ | ✅ | ✅ | ✅ |
| Alternative speed (turtle mode) | ✅ | ✅ | ❌ ¹ | ❌ ¹ |
| Verify the data | ✅ | ✅ | ✅ | ❌ ² |
| Smart, manual and automatic renaming | ✅ | ✅ | ✅ | ❌ ³ |

¹ Neither Deluge nor Download Station has an alternative speed mode. Regular speed limits do work on both.

² Download Station exposes no way to re-check the data of a task.

³ The Download Station API has no field for the name of a task or of the files it contains: the only editable thing about a task is its destination. The content can be renamed on disk with File Station, but then the task no longer finds its files and breaks on the next check DSM makes (a reboot of the NAS, of the package, or a pause and resume), so the bot does not do it. It is the same limitation the DSM interface itself has, where a download cannot be renamed either.

## Synology Download Station

To use Download Station (`TORRENT_CLIENT=download_station`) the bot talks to the DSM web API with a NAS user, the same kind of account you use to log in to DSM. The recommended setup is to **create a user dedicated to the bot**:

1. In DSM, go to `Control Panel` → `User & Group` → `Create`.
2. Give it a password and **do not enable two step verification** on it: the login API rejects the account without a TOTP code and the bot will not be able to start.
3. In the application permissions, grant it access to **Download Station** and to **File Station**. File Station is used to move data, to delete it, to create destination folders that do not exist yet and to read the free space, things the Download Station API cannot do.
4. It does not need to be an administrator, nor to have write access to any shared folder other than the download ones.

Those credentials go into `TORRENT_CLIENT_USER` and `TORRENT_CLIENT_PASSWORD`. The port is the DSM one: 5000 over http or 5001 over https (set `TORRENT_CLIENT_PROTOCOL=https` if you use 5001).

> [!NOTE]
> Download Station download paths are relative to the shared folder, not absolute: they are written as `Downloads` or `video/movies`, not `/volume1/Downloads`. The bot creates the folder you type if it does not exist yet, unless it is a shared folder, which can only be created from DSM.

> [!NOTE]
> The bot lists **every** Download Station task, including the HTTP or FTP downloads you added from DSM. For those, Download Station considers the download closed as soon as it ends and no longer allows changing its destination: if you try to move one, the bot says so and leaves the files alone.

> [!NOTE]
> Moving a torrent that is seeding leaves it waiting for a few seconds and runs a quick check before it seeds again. That is how DSM behaves when its destination changes, not an error.

## Configuration via Docker Compose variables

| KEY  | MANDATORY | VALUE |
|:------------- |:---------------:| :-------------|
|TELEGRAM_TOKEN |✅| Bot token |
|TELEGRAM_ADMIN |✅| Administrator's chatId (you can get it by talking to the [Rose](https://t.me/MissRose_bot) bot and typing /id). Supports multiple administrators separated by commas. For example 12345,54431,55944 |
|TELEGRAM_GROUP |❌| Group chatId. If this bot is going to be part of a group, you need to specify the chatId of that group. The bot must be an administrator of the group |
|TELEGRAM_THREAD |❌| Topic thread inside a supergroup; numeric value (2,3,4..). Default 1. Used together with the TELEGRAM_GROUP variable |
|TZ |✅| Timezone (e.g. Europe/Madrid) |
|LANGUAGE |❌| Language, can be ES / EN. Default ES (Spanish) |
|TORRENT_CLIENT |❌| Torrent client: `transmission`, `qbittorrent`, `deluge` or `download_station`. Default transmission |
|TORRENT_CLIENT_HOST |✅| Host or IP where the torrent client is running |
|TORRENT_CLIENT_PORT |❌| Torrent client port. Default 9091 (Transmission), 8080 (qBittorrent), 8112 (Deluge Web UI) or 5000/5001 (Synology DSM) |
|TORRENT_CLIENT_USER |❌| Torrent client user, if it has authentication |
|TORRENT_CLIENT_PASSWORD |❌| Torrent client password, if it has authentication |
|TORRENT_CLIENT_PROTOCOL |❌| Connection protocol, http or https. Default http |
|TORRENT_CLIENT_RPC_PATH |❌| RPC path. Default /transmission/rpc (Transmission) or /webapi (Synology) |
|TORRENTS_PER_PAGE |❌| Number of torrents per page in the lists. Default 10 |
|DASHBOARD_REFRESH_SECONDS |❌| Seconds between automatic dashboard refreshes. Default 2 |
|DASHBOARD_REFRESH_DURATION |❌| Seconds the automatic dashboard refresh lasts. Default 60 |

## Docker-Compose example for normal execution

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
            - /path/of/your/choice:/config # Persistent bot settings
```

## Notes
> [!NOTE]
> The bot does not need access to the downloaded files: it communicates with the torrent client via RPC only. The only volume you need to map is `/config`, where the bot stores its settings so they persist across restarts.

> [!NOTE]
> If Transmission runs on the same machine as the bot but outside Docker, use `host.docker.internal` as `TORRENT_CLIENT_HOST` (on Linux add `extra_hosts: ["host.docker.internal:host-gateway"]`).

---
## Developers only

### Running with local code

To run it locally and test new code changes, rename the `.env.example` file to `.env` with the values needed for execution.
You need to set a valid `TELEGRAM_TOKEN` and `TELEGRAM_ADMIN`, different from the ones used in the normal execution.

The folder structure should look like:

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

To start it, run in that path: `docker compose -f docker-compose.yaml up -d --build --force-recreate` using the `Dockerfile_local`.

It can also be run directly with Python:

```bash
pip install -r requirements.txt
set -a && source .env && set +a
LOCALE_PATH=./locale python3 torrent-controller-bot.py
```
