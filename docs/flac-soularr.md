# FLAC route: Lidarr + slskd + soularr

The app's "Buscar en FLAC" button calls `POST /api/flac`, which adds the artist to
Lidarr with the Lossless quality profile and triggers a search. Lidarr then needs a
way to actually fetch lossless files from Soulseek. That bridge is **soularr**
(https://github.com/mrusse/soularr): it reads Lidarr's wanted list, searches slskd
(the Soulseek daemon), downloads matches, and hands them back to Lidarr for import.

This document only covers adding soularr. Lidarr (`:8686`) and slskd (`:5030`) are
already running.

## 1. Add slskd as a download client in Lidarr

soularr imports through Lidarr, so Lidarr must know about the downloads folder that
slskd and soularr share. Confirm both slskd and Lidarr see the same path
(`/data/downloads` in this setup).

Get the slskd API key from its config (`/home/edgar/slskd/config/slskd.yml`, under
`web.authentication.api_keys`) or set one there.

## 2. docker-compose for soularr

Create `/home/edgar/soularr/docker-compose.yml`:

```yaml
services:
  soularr:
    image: mrusse08/soularr:latest
    container_name: soularr
    restart: unless-stopped
    hostname: soularr
    environment:
      - SCRIPT_INTERVAL=300      # seconds between runs
      - TZ=America/Mexico_City
    volumes:
      - /home/edgar/soularr/config:/data
      - /data/downloads:/downloads
```

The `/data/downloads` mount must be the same host path that slskd writes to and that
Lidarr reads from, so imports work.

## 3. soularr config

Create `/home/edgar/soularr/config/config.ini`:

```ini
[Lidarr]
api_key = <LIDARR_API_KEY>
host_url = http://192.168.0.41:8686
download_dir = /downloads

[Slskd]
api_key = <SLSKD_API_KEY>
host_url = http://192.168.0.41:5030
download_dir = /downloads
delete_searches = True

[Release Settings]
use_most_common_tracknum = True
allow_multi_disc = True
accepted_countries = Europe,Japan,United States,United Kingdom,Canada,Worldwide
accepted_formats = FLAC

[Search Settings]
search_timeout = 5000
maximum_peer_queue = 50
minimum_peer_upload_speed = 0
allow_missing_tracks = False

[Logging]
level = INFO
```

Use the same Lidarr API key already configured in `sonora-server/.env`
(`LIDARR_API_KEY`) and the slskd key from step 1.

## 4. Start it

```
cd /home/edgar/soularr
docker compose up -d
docker logs -f soularr
```

soularr runs every `SCRIPT_INTERVAL` seconds: it picks up whatever Lidarr has marked
as monitored+missing (which is exactly what `/api/flac` enqueues), searches slskd,
downloads FLAC, and Lidarr imports it into `/music`. Navidrome then indexes it.

## Flow summary

app "Buscar en FLAC" -> `POST /api/flac` -> Lidarr (add artist, Lossless, search)
-> soularr picks up the wanted albums -> slskd downloads FLAC from Soulseek
-> Lidarr imports into `/music` -> Navidrome indexes it.
