# Local STAC API for point clouds and panoramas

A minimal self-hosted STAC API, built the same way Earth Search is: a
Postgres/PostGIS database with the [pgstac](https://github.com/stac-utils/pgstac)
schema, fronted by [stac-fastapi-pgstac](https://github.com/stac-utils/stac-fastapi-pgstac).
A small ingest script walks a folder, finds LAS/LAZ point clouds and
panoramic photos, pulls out the metadata STAC needs, and loads whatever
qualifies into the database.

Once running, QGIS should connect to it exactly like it connects to Earth
Search - the same STAC-API root document, same `conformsTo`, same
search/filter UI.

## 1. Start the services

```bash
cp .env.example .env
docker compose up -d
```

This starts three containers:

- **pgstac** - Postgres + PostGIS with the pgstac schema (migrations run
  automatically on first start)
- **stac-fastapi** - the HTTP API, on `http://localhost:8080`
- **fileserver** - serves `./data` (or whatever `DATA_DIR` points to) over
  plain HTTP on `http://localhost:8081`, so that STAC asset links are
  actually fetchable by pgstac/QGIS. Point your LAS/LAZ files and
  panoramas somewhere under this folder.

Check the API came up:

```bash
curl http://localhost:8080/
```

You should get back a landing-page document that looks just like the
Earth Search one, with `links` for `search`/`collections`/`conformance`.

## 2. Put some data in `./data`

Drop `.las`/`.laz` files and panorama photos (JPEG/PNG, roughly 2:1
aspect ratio) anywhere under `./data`, in whatever subfolders you like.

## 3. Ingest

```bash
uv sync

uv run ingest/scan_and_ingest.py ./data \
  --dsn postgresql://username:password@localhost:5439/postgis \
  --asset-base-url http://localhost:8081
```

`uv sync` reads `pyproject.toml`, creates `.venv/`, and writes `uv.lock` on
first run. Commit `uv.lock` once it exists so everyone (and CI) resolves
the same dependency versions.

Add `--dry-run` first to see what would be ingested (and what gets
skipped, and why) without touching the database.

Each file is only ingested if it has what STAC needs:

- **LAS/LAZ**: a CRS embedded in the header, and a valid bounding box.
  Files with neither are skipped and logged.
- **Panoramas**: roughly 2:1 aspect ratio (equirectangular), plus GPS
  EXIF data (`GPSLatitude`/`GPSLongitude`). Files without GPS are
  skipped and logged - there's no way to place them on a map otherwise.

Point clouds land in a `pointclouds` collection, panoramas in a
`panoramas` collection (override with `--pointcloud-collection` /
`--panorama-collection`).

## 4. Connect QGIS

Data Source Manager → STAC → new connection → `http://localhost:8080`.
Use Filters to draw a bounding box and search - footprints for point
clouds and location points for panoramas should show up on the canvas,
same as with Earth Search.

## Known limitations / next steps

- **`pypgstac` and the `pgstac` Docker image must be on the exact same
  version.** pgstac checks this strictly and refuses to run otherwise -
  see `pyproject.toml` (`pypgstac==0.9.8`) and `docker-compose.yml`
  (`ghcr.io/stac-utils/pgstac:v0.9.8`). If you bump one, bump the other
  to match, then `uv sync` and `docker compose up -d --force-recreate
  pgstac`.
- **COPC detection is a filename heuristic** (`*.copc.laz`), not a real
  parse of the COPC info VLR. If you need certainty about whether a file
  will stream in QGIS as a point cloud layer vs. require a full download
  first, verify with `pdal info --metadata` before relying on this.
- **Collection extents are computed from the actual items being loaded**,
  merged with whatever the collection already covered from previous runs
  - so re-running the script against new files grows the extent rather
  than shrinking or overwriting it. No manual extent-recompute step
  needed.
- **Panorama detection is a heuristic** (aspect ratio + GPS EXIF), not a
  check of the Google `GPano` XMP tags that mark a file as a genuine
  equirectangular capture. Fine for a first pass; worth tightening if
  you're ingesting photos from mixed sources.
- **The `fileserver` container is a placeholder.** Swap it for whatever
  you actually use to serve files in production (nginx, S3 + signed
  URLs, etc.) - just make sure `--asset-base-url` matches wherever the
  files really end up.
- Re-running the ingest script is safe - items are upserted by ID, so
  changed metadata gets updated rather than duplicated.
