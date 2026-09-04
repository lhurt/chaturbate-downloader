# CB Stream Saver

A self-hosted FastAPI web application for downloading live HLS streams from
Chaturbate rooms. Runs locally, exposes a small browser UI, and handles the
full pipeline: URL extraction, segment downloading with automatic token
refresh, parallel video + audio fetching, and final remux into an MP4 with
ffmpeg.

This project is intended for personal, local use only.

---

## Features

- **Web UI** — start, monitor, and stop downloads from the browser.
- **Tracked streamers dashboard** — keeps a SQLite-backed registry of every
  username you've downloaded. The server polls room status every 60 seconds
  and shows live thumbnails, online/offline state, and quick download or
  delete actions.
- **Per-streamer auto recording** — enable `Auto record` on a tracked card to
  start recording automatically whenever the background poller finds that
  streamer live, even when the browser is closed.
- **Clickable profile links** — every tracked card links the thumbnail and
  username directly to `https://chaturbate.com/<username>/`.
- **Theme controls** — switch between Light, Auto, and Dark modes.
- **Multiple concurrent downloads** — each stream runs in its own task under
  a central `DownloadManager`.
- **Robust URL extraction** — four fallback strategies (`chatvideocontext`
  API, embedded `initialRoomDossier` JSON, `get_edge_hls_url_ajax` CSRF, and
  a regex sweep on the page HTML).
- **Audio + video muxing** — Chaturbate serves LL-HLS with split video and
  audio playlists. Both tracks are downloaded in parallel and muxed with
  ffmpeg while preserving source timestamps.
- **Token refresh on 403** — HLS tokens are single-use and short-lived, so
  the downloader re-extracts the URL on `403` responses (up to 10 refreshes
  per track).
- **Optional max duration** — cap a download at N minutes.
- **File management endpoints** — list, download, and delete finished files.
- **Debug endpoints** — inspect extraction and playlist parsing without
  committing to a full download. Token-bearing URLs are redacted in responses.
- **Path-traversal safe** — all filenames are resolved inside the
  `downloads/` directory.
- **Local request protection** — state-changing endpoints reject cross-site
  browser requests and unconfigured origins.

---

## How it works

```
  Browser UI  ──►  FastAPI (app.py)  ──►  DownloadManager
                                              │
                                              ▼
                                        extractor.py
                                    (4 strategies for HLS URL)
                                              │
                                              ▼
                                          hls.py
                               (parses master playlist,
                                downloads video + audio in parallel,
                                refreshes token on 403)
                                              │
                                              ▼
                                       converter.py
                                     (ffmpeg mux + A/V sync)
                                              │
                                              ▼
                                    downloads/<user>_<ts>.mp4
```

Each HLS master playlist contains video variants and a separate audio media
playlist. `HLSDownloader` picks the highest-bandwidth video variant, finds
its matching audio group, and streams both to temporary `.mp4` files. When
the download ends (user stop, timeout, stream going offline, or too many
errors), `mux_video_audio` combines them into a single MP4 with
`-movflags +faststart`.

---

## Requirements

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **ffmpeg** and **ffprobe** on `PATH` (required for final MP4 muxing and
  timestamp/duration diagnostics)

Python dependencies (declared in `pyproject.toml`):

- `fastapi`, `uvicorn[standard]`
- `httpx`
- `m3u8`
- `jinja2`
- `python-multipart`

Development dependencies:

- `pytest`

---

## Installation

Clone the repo and run the setup script:

```bash
./setup.sh
```

The script verifies `uv` and Python 3.10+, runs `uv sync` to install
dependencies, and warns if `ffmpeg` is missing.

Install `ffmpeg` if needed:

```bash
# Arch
sudo pacman -S ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

---

## Running

```bash
uv run python app.py
```

Then open [http://localhost:8000](http://localhost:8000).

### Environment variables

| Variable       | Default                  | Purpose                                       |
| -------------- | ------------------------ | --------------------------------------------- |
| `HOST`         | `127.0.0.1`              | Bind address for uvicorn                      |
| `PORT`         | `8000`                   | Port                                          |
| `CORS_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000,http://[::1]:8000` | Comma-separated list of allowed UI origins for CORS and state-changing requests |
| `CB_PROXY_URL` | unset                    | Optional HTTP proxy URL for Chaturbate and HLS CDN requests |
| `CONTACT_SHEET_INTERVAL_SECONDS` | `60`   | Seconds between thumbnail frames in a generated contact sheet |
| `CONTACT_SHEET_TILE_WIDTH` | `160`        | Width in pixels of each thumbnail tile |
| `CONTACT_SHEET_COLUMNS` | `10`            | Number of tiles per row in the contact sheet grid |

Example:

```bash
HOST=0.0.0.0 PORT=9000 uv run python app.py
```

If the assigned Chaturbate CDN edge times out from your network, route the app
through an HTTP proxy that can reach `*.live.mmcdn.com`:

```bash
CB_PROXY_URL=http://user:pass@proxy.example:8080 uv run python app.py
```

With Docker Compose, put the same value in `.env`:

```env
CB_PROXY_URL=http://user:pass@proxy.example:8080
```

### Running with Docker Compose from Docker Hub

The published image is available at
[sojiroh/cb-stream-saver](https://hub.docker.com/repository/docker/sojiroh/cb-stream-saver).

Create a `.env` file from the example and set `CORS_ORIGINS` to the exact URL
you will use in the browser:

```bash
cp .env.example .env
```

Example `.env` for a NAS on `192.168.1.50`:

```env
PORT=8000
CORS_ORIGINS=http://192.168.1.50:8000
```

Start the service using the Docker Hub compose file:

```bash
docker compose -f docker-compose.hub.yml up -d
```

Then open `http://192.168.1.50:8000`, replacing the address with your NAS IP
or local hostname. Finished MP4 files are stored in the host `./downloads`
directory through the compose volume.

To update to the latest published image:

```bash
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d
```

### Publishing with GitHub Actions

The repository includes `.github/workflows/docker-publish.yml` to publish the
Docker image and sync this README to Docker Hub automatically.

Configure these repository secrets in GitHub before using the workflow:

| Secret               | Purpose                                      |
| -------------------- | -------------------------------------------- |
| `DOCKERHUB_USERNAME` | Docker Hub username with access to the repo  |
| `DOCKERHUB_TOKEN`    | Docker Hub access token or password          |

The workflow runs on every push to `main`, on tags matching `v*.*.*`, and when
triggered manually from the GitHub Actions tab.

Image tags produced by the workflow:

| Git ref              | Docker tags                                  |
| -------------------- | -------------------------------------------- |
| Push to `main`       | `latest`, `sha-<commit>`                     |
| Tag `v1.2.3`         | `1.2.3`, `1.2`, `sha-<commit>`              |

---

## Web UI

The main page (`templates/index.html`) shows four sections:

1. **New Download** — enter a username and optionally set a max duration in
   minutes. Output is MP4.
2. **Tracked Streamers** — a dashboard of every username you've ever
   downloaded. Cards show the latest thumbnail, live/offline/private status,
   last-seen time, and per-card download or delete actions. Thumbnails and
   names link to the room's Chaturbate profile in a new tab. The list is
   refreshed automatically every 30 seconds.
3. **Active Downloads** — live progress cards (segments, speed, elapsed
   time, status) with per-download stop and a global "Stop All" button.
4. **Completed Downloads** — list of finished files with download and
   delete actions.

Downloads land in `./downloads/<username>_<YYYY-MM-DD_HH-MM-SS>.mp4`.

---

## API reference

All endpoints are under the FastAPI app at `/`.

### Downloads

| Method | Path                                | Description                                    |
| ------ | ----------------------------------- | ---------------------------------------------- |
| POST   | `/api/download/start`               | Start a download (query: `username`, `output_format`, `max_duration` in minutes) |
| POST   | `/api/download/stop/{username}`     | Stop one download gracefully (waits for mux)   |
| POST   | `/api/download/stop-all`            | Stop all active downloads                      |
| GET    | `/api/download/status`              | List status of every tracked download          |
| GET    | `/api/download/status/{username}`   | Status of one download                         |
| GET    | `/api/download/file/{username}`     | Legacy helper: stream the latest completed file for `{username}` |

### Files

| Method | Path                         | Description                               |
| ------ | ---------------------------- | ----------------------------------------- |
| GET    | `/api/downloads/list`             | List completed `.mp4` files in `downloads/` |
| GET    | `/api/downloads/file/{filename}`  | Stream one exact completed file by filename |
| DELETE | `/api/downloads/{filename}`       | Delete a completed file (path-traversal safe) |

### Tracked Streamers

| Method | Path                               | Description                                    |
| ------ | ---------------------------------- | ---------------------------------------------- |
| GET    | `/api/tracked`                     | List all tracked streamers with status, last-seen times, and active-download flags |
| PATCH  | `/api/tracked/{username}/auto-download` | Enable or disable automatic recording with the `enabled` query parameter |
| DELETE | `/api/tracked/{username}`          | Remove a username from the tracked registry    |
| GET    | `/api/thumbnail/{username}`        | Fetch the current room thumbnail (cached for 30 seconds) |

### Debug

| Method | Path                              | Description                                                |
| ------ | --------------------------------- | ---------------------------------------------------------- |
| GET    | `/api/debug/extract/{username}`   | Run the extractor for a room and return the URL + logs    |
| GET    | `/api/debug/playlist/{username}`  | Fetch and parse the master + selected variant playlists   |

Username validation (`^[a-zA-Z0-9_]{1,50}$`) is applied on every endpoint
that accepts one.

State-changing endpoints (`POST`/`DELETE`) reject browser cross-site requests
and origins outside `CORS_ORIGINS`. Keep `CORS_ORIGINS` in sync if you change
`HOST`/`PORT` or put the UI behind a different local origin.

---

## Project structure

```
chaturbate/
├── app.py                 # FastAPI app, routes, validation, lifespan, background poller
├── downloader/
│   ├── __init__.py        # Public exports
│   ├── extractor.py       # 4 strategies to pull a fresh HLS URL
│   ├── hls.py             # LL-HLS downloader (video + audio, token refresh)
│   ├── converter.py       # ffmpeg remux + video/audio mux with A/V sync
│   ├── manager.py         # DownloadManager: tasks, state, lifecycle
│   └── tracker.py         # SQLite-backed registry of tracked streamers
├── templates/
│   └── index.html         # Single-page UI
├── static/
│   ├── app.js             # Frontend logic (polling, forms, file list)
│   ├── tracked.js         # Tracked-streamer dashboard (cards, thumbnails, status)
│   ├── tracked.css        # Styles for the tracked streamers section
│   ├── theme.js           # Light / Auto / Dark theme controls
│   └── style.css
├── tests/
│   └── test_backend.py    # Backend safety/regression tests
├── downloads/             # Output directory (gitignored)
├── pyproject.toml         # uv / PEP 621 project definition
├── setup.sh               # One-shot environment check and install
└── uv.lock
```

---

## Notes and caveats

- **Single-use tokens.** Chaturbate's HLS URLs are session-bound. The
  extractor never probes the URL itself — the first HTTP GET must be made
  by the downloader, or the token gets burned. API responses and logs redact
  token-bearing URLs where possible.
- **A/V drift.** Video and audio come from separate HLS playlists and
  occasionally start at slightly different timestamps. `mux_video_audio`
  probes both with `ffprobe` for diagnostics and uses `-copyts` /
  `-start_at_zero` so ffmpeg preserves their relative timestamps.
- **Mux fallback.** If muxing fails, the video-only file is kept and
  `error_message` is set to `"Mux failed, file is video-only"`.
- **Graceful stop.** `stop_download` sets an event instead of cancelling
  the task, so the current segment batch finishes and the mux runs before
  the task exits. There's a 120 s watchdog if the task hangs.
- **Tracked streamer data** lives in `downloads/tracked.db` (or the
  directory pointed to by `DOWNLOADS_DIR`). It stores usernames, download
  counts, last-seen timestamps, and the most recent room status captured by
  the background poller. The per-streamer auto-record preference is persisted
  in the same database and defaults to disabled.
- **Local-only security model.** The server is intended for local use. It
  rejects obvious browser cross-site writes via `Sec-Fetch-Site` and origin
  checks on state-changing endpoints, but it does not provide authentication.
  Do not expose it to the public internet. The default `HOST` is `127.0.0.1`
  for this reason.

---

## Tests

```bash
uv run pytest
```

The current tests cover validation, path/file safety, completed-file listing,
tracked streamer APIs, origin checks, URL redaction, download-manager
start/stop race regressions, and ffmpeg mux cancellation / timeout behavior.

---

## Troubleshooting

- **"Could not get stream URL. Is the room online?"** — All four
  extraction strategies returned nothing. The room is offline, banned, or
  Chaturbate changed its API. Hit `/api/debug/extract/{username}` to see
  which strategy failed and why.
- **403s in the logs** — Normal; the downloader will refresh the token
  automatically up to 10 times per track. Persistent 403s usually mean the
  room went private or offline.
- **`ffmpeg not found in PATH`** — Install `ffmpeg` (see above). Without
  it, downloads still run but the final mux step will fail.
- **No audio in the output** — Check the logs for
  `No matching audio found for group`. Some variants reference audio
  groups that aren't in the master playlist; the downloader keeps the
  video-only file in that case.

---

## License / disclaimer

This project is provided as-is for personal, local, educational use. Respect
Chaturbate's terms of service and the rights of performers. Do not
redistribute downloaded content.
