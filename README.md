# Sonora Server

[![Build](https://img.shields.io/github/actions/workflow/status/EdgarAldair/sonora/ci.yml?branch=main)](https://github.com/EdgarAldair/sonora/actions)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org)
[![Repo size](https://img.shields.io/github/repo-size/EdgarAldair/sonora)](https://github.com/EdgarAldair/sonora)
[![Stars](https://img.shields.io/github/stars/EdgarAldair/sonora)](https://github.com/EdgarAldair/sonora/stargazers)

Self-hosted backend for [Sonora](https://apps.apple.com/us/app/sonora-media-player/id6792233706). It
turns your own machine into a personal music streaming service: search and stream,
artist/album pages, synced lyrics, per-user playlists and library, and an optional
Alexa skill. FastAPI + yt-dlp + ffmpeg, packaged with Docker.

## Quick start

```
git clone https://github.com/EdgarAldair/sonora sonora-server
cd sonora-server
cp .env.example .env      # edit it: set SONORA_PASSWORD, mount your music, etc.
docker compose up -d --build
```

The API listens on `http://localhost:8100`. Point the Sonora app at it (or expose
it over HTTPS with a reverse proxy / tunnel).

Put your music in `./music`, or set `MUSIC_DIR` in `.env` to your library path.

## Configuration

All settings are environment variables; see `.env.example`. Do not commit `.env`.

- `SONORA_USER` / `SONORA_PASSWORD`: the account seeded on first run.
- `COOKIES_FILE`: optional YouTube cookies to reduce bot checks.
- `LIDARR_*`: optional Lidarr integration for lossless requests.
- `ALEXA_*` / `PUBLIC_BASE_URL`: optional Alexa skill (see below).

## Alexa skill (optional)

The server exposes a custom Alexa skill (AudioPlayer) at `/alexa`, with OAuth
Account Linking so each person uses their own Sonora account.

1. Create a Custom skill (Music & Audio, Spanish or your locale) in the Alexa
   developer console. Import `docs/alexa-interaction-model.json` in the JSON editor
   and enable the Audio Player interface.
2. Endpoint: HTTPS, `https://YOUR_HOST/alexa`. If your certificate is a wildcard
   for a parent domain, choose the wildcard sub-domain option.
3. Account Linking: Auth Code Grant. Authorization URI `https://YOUR_HOST/oauth/authorize`,
   Access Token URI `https://YOUR_HOST/oauth/token`, and the `ALEXA_CLIENT_ID` /
   `ALEXA_CLIENT_SECRET` from your `.env`.
4. Set `ALEXA_SKILL_ID` and `PUBLIC_BASE_URL` in `.env` and redeploy.

Each user links from the Alexa app, then says things like "ask Sonora to play
reggaeton", "play the album The Better Life", or "play the playlist road trip".

## Tests / CI

CI runs a syntax check on every push and pull request (see
`.github/workflows/ci.yml`).

## License

MIT. Copyright (c) Edgar Saenz.
