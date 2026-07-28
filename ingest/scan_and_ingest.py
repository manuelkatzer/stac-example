#!/usr/bin/env python3
"""
Scan a folder for LAS/LAZ point clouds and panoramic images, extract the
metadata STAC needs (CRS + bounding box, or GPS location, plus a capture
timestamp), and load whatever qualifies straight into a pgstac database.

Files missing the required metadata are skipped and logged - never
partially ingested.

Usage:
    python scan_and_ingest.py /path/to/data \
        --dsn postgresql://username:password@localhost:5439/postgis \
        --asset-base-url http://localhost:8081

`folder` should be the same directory (or a subfolder of it) that the
fileserver container in docker-compose.yml is serving, since the asset
hrefs written into STAC are built as `{asset-base-url}/{relative-path}`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import laspy
import psycopg
from PIL import Image
from PIL.ExifTags import GPSTAGS, TAGS
from pyproj import CRS, Transformer
from pypgstac.db import PgstacDB
from pypgstac.load import Loader, Methods

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("stac-ingest")

LAS_EXTENSIONS = {".las", ".laz"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def base_collection(collection_id: str, description: str) -> dict[str, Any]:
    # Placeholder only - real extent is computed from actual items in main()
    # and merged with whatever the collection already covers, before this
    # gets upserted. Never left in place on a collection that has items.
    return {
        "id": collection_id,
        "type": "Collection",
        "stac_version": "1.0.0",
        "description": description,
        "license": "proprietary",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [[None, None]]},
        },
        "links": [],
    }


def batch_extent(items: list[dict[str, Any]]) -> tuple[list[float], list[str]] | None:
    """Bounding box and datetime range covering everything in `items`."""
    if not items:
        return None
    bboxes = [item["bbox"] for item in items]
    bbox = [
        min(b[0] for b in bboxes), min(b[1] for b in bboxes),
        max(b[2] for b in bboxes), max(b[3] for b in bboxes),
    ]
    datetimes = sorted(item["properties"]["datetime"] for item in items)
    return bbox, [datetimes[0], datetimes[-1]]


def fetch_existing_extent(dsn: str, collection_id: str) -> dict[str, Any] | None:
    """The current extent object for a collection already in pgstac, if any."""
    try:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT content->'extent' FROM collections WHERE id = %s", (collection_id,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Could not read existing extent for collection %r (%s) - "
            "starting fresh from this batch only",
            collection_id, exc,
        )
        return None


def merge_extents(
    existing: dict[str, Any] | None,
    bbox: list[float],
    time_range: list[str],
) -> dict[str, Any]:
    """Union an existing collection extent with a new batch's extent."""
    if existing is None:
        merged_bbox, merged_time = bbox, time_range
    else:
        old_bbox = existing.get("spatial", {}).get("bbox", [[None]])[0]
        old_time = existing.get("temporal", {}).get("interval", [[None, None]])[0]

        if old_bbox and old_bbox[0] is not None and old_bbox != [-180, -90, 180, 90]:
            merged_bbox = [
                min(old_bbox[0], bbox[0]), min(old_bbox[1], bbox[1]),
                max(old_bbox[2], bbox[2]), max(old_bbox[3], bbox[3]),
            ]
        else:
            merged_bbox = bbox

        old_start, old_end = (old_time + [None, None])[:2]
        candidates_start = [t for t in (old_start, time_range[0]) if t is not None]
        candidates_end = [t for t in (old_end, time_range[1]) if t is not None]
        merged_time = [
            min(candidates_start) if candidates_start else None,
            max(candidates_end) if candidates_end else None,
        ]

    return {
        "spatial": {"bbox": [merged_bbox]},
        "temporal": {"interval": [merged_time]},
    }


def iter_files(root: Path, extensions: set[str]) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in extensions:
            yield path


def sanitize_collection_id(name: str) -> str:
    """Turn a folder name into a clean, URL/id-safe collection id."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug or "unnamed"


def load_manifest(path: Path) -> dict[str, dict[str, int]]:
    """rel_path -> {mtime_ns, size} for every file successfully ingested so far."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read manifest %s (%s) - starting fresh", path, exc)
        return {}


def save_manifest(path: Path, manifest: dict[str, dict[str, int]]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


def file_fingerprint(path: Path) -> dict[str, int]:
    st = path.stat()
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


# ---------------------------------------------------------------------------
# Point clouds
# ---------------------------------------------------------------------------

def looks_like_copc(path: Path) -> bool:
    # Heuristic only: files produced by common COPC tools (PDAL, untwine,
    # copc-lib) are conventionally named "*.copc.laz". This does not parse
    # the COPC info VLR, so double check with `pdal info` if you need
    # certainty before relying on QGIS's streaming point-cloud support.
    return path.name.lower().endswith(".copc.laz")


def read_las_metadata(path: Path, assume_crs: CRS | None = None) -> dict[str, Any] | None:
    try:
        with laspy.open(path) as reader:
            header = reader.header
            crs = header.parse_crs()
            if crs is None:
                # laspy.open() only reads regular VLRs, not EVLRs. LAS 1.4
                # point formats 6-10 (which COPC requires) store their CRS
                # as WKT, and WKT often doesn't fit in a regular VLR
                # (65535-byte limit) so it ends up in an EVLR instead.
                # Force-read the EVLRs and try again before giving up.
                reader.evlrs  # noqa: B018 - accessing the property reads & caches them
                crs = header.parse_crs()
    except Exception as exc:  # noqa: BLE001
        log.warning("Skipping %s: could not read LAS/LAZ header (%s)", path, exc)
        return None

    if crs is None:
        if assume_crs is not None:
            log.info(
                "%s: no usable CRS embedded (VLR/EVLR present but empty) - using --assume-crs %s",
                path, assume_crs,
            )
            crs = assume_crs
        else:
            log.warning(
                "Skipping %s: no usable CRS embedded (VLR/EVLR present but empty) - "
                "pass --assume-crs if you know what it should be",
                path,
            )
            return None

    mins, maxs = header.mins, header.maxs
    if mins is None or maxs is None:
        log.warning("Skipping %s: no bounding box in header", path)
        return None

    try:
        transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        corners = [
            (mins[0], mins[1]), (maxs[0], mins[1]),
            (maxs[0], maxs[1]), (mins[0], maxs[1]),
        ]
        lonlat_corners = [list(transformer.transform(x, y)) for x, y in corners]
    except Exception as exc:  # noqa: BLE001
        log.warning("Skipping %s: could not reproject CRS %s to EPSG:4326 (%s)", path, crs, exc)
        return None

    lons = [c[0] for c in lonlat_corners]
    lats = [c[1] for c in lonlat_corners]
    bbox = [min(lons), min(lats), max(lons), max(lats)]
    ring = lonlat_corners + [lonlat_corners[0]]

    creation_date = header.creation_date
    capture_dt = (
        dt.datetime(creation_date.year, creation_date.month, creation_date.day, tzinfo=dt.timezone.utc)
        if creation_date
        else dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    )

    return {
        "bbox": bbox,
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "datetime": capture_dt,
        "point_count": int(header.point_count),
        "is_copc": looks_like_copc(path),
        "crs_assumed": crs is assume_crs and assume_crs is not None,
        "crs": crs,
    }


def proj_properties(crs: CRS) -> dict[str, Any]:
    """STAC `proj` extension fields (v2.0) describing an item's native CRS.

    `proj:code` (e.g. "EPSG:5682") is the standard, human/machine-friendly
    identifier when the CRS has one. `proj:wkt2` is always included too, as
    a robust fallback for CRSes without a registered code (e.g. custom or
    unregistered systems) and for clients that prefer exact WKT over a
    code lookup.
    """
    props: dict[str, Any] = {"proj:wkt2": crs.to_wkt()}
    authority = crs.to_authority()
    if authority is not None:
        props["proj:code"] = f"{authority[0]}:{authority[1]}"
    return props


def build_las_item(path: Path, meta: dict[str, Any], collection: str, asset_base_url: str, data_root: Path) -> dict[str, Any]:
    rel = path.relative_to(data_root).as_posix()
    media_type = "application/vnd.laszip+copc" if meta["is_copc"] else "application/vnd.laszip"
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/projection/v2.0.0/schema.json",
        ],
        "id": path.stem,
        "collection": collection,
        "geometry": meta["geometry"],
        "bbox": meta["bbox"],
        "properties": {
            "datetime": meta["datetime"].isoformat(),
            "pc:count": meta["point_count"],
            "crs:assumed": meta["crs_assumed"],
            **proj_properties(meta["crs"]),
        },
        "assets": {
            "data": {
                "href": f"{asset_base_url.rstrip('/')}/{rel}",
                "type": media_type,
                "roles": ["data"],
                "title": path.name,
            }
        },
        "links": [],
    }


# ---------------------------------------------------------------------------
# Panoramas
# ---------------------------------------------------------------------------

def _to_degrees(value) -> float:
    d, m, s = value
    return float(d) + float(m) / 60 + float(s) / 3600


def read_pano_metadata(path: Path) -> dict[str, Any] | None:
    try:
        img = Image.open(path)
        width, height = img.size
        exif = img.getexif()
    except Exception as exc:  # noqa: BLE001
        log.warning("Skipping %s: could not read image (%s)", path, exc)
        return None

    # Classify rather than reject: true equirectangular panoramas are ~2:1,
    # but a perfectly ordinary geotagged photo (any aspect ratio) is just
    # as valid to place on a map - it just isn't a 360 panorama.
    is_equirectangular = height > 0 and abs(width / height - 2.0) <= 0.05

    gps_ifd = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else {}
    if not gps_ifd:
        log.warning("Skipping %s: no GPS EXIF data present", path)
        return None

    gps = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
    if "GPSLatitude" not in gps or "GPSLongitude" not in gps:
        log.warning("Skipping %s: GPS EXIF present but missing lat/lon", path)
        return None

    lat = _to_degrees(gps["GPSLatitude"])
    if gps.get("GPSLatitudeRef") == "S":
        lat = -lat
    lon = _to_degrees(gps["GPSLongitude"])
    if gps.get("GPSLongitudeRef") == "W":
        lon = -lon

    tags = {TAGS.get(k, k): v for k, v in exif.items()}
    date_str = tags.get("DateTimeOriginal") or tags.get("DateTime")
    capture_dt = None
    if date_str:
        try:
            capture_dt = dt.datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            capture_dt = None
    if capture_dt is None:
        capture_dt = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)

    return {
        "lon": lon,
        "lat": lat,
        "datetime": capture_dt,
        "width": width,
        "height": height,
        "photo_type": "equirectangular" if is_equirectangular else "perspective",
    }


def build_pano_item(path: Path, meta: dict[str, Any], collection: str, asset_base_url: str, data_root: Path) -> dict[str, Any]:
    rel = path.relative_to(data_root).as_posix()
    media_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": path.stem,
        "collection": collection,
        "geometry": {"type": "Point", "coordinates": [meta["lon"], meta["lat"]]},
        "bbox": [meta["lon"], meta["lat"], meta["lon"], meta["lat"]],
        "properties": {
            "datetime": meta["datetime"].isoformat(),
            "panorama:type": meta["photo_type"],
            "panorama:width": meta["width"],
            "panorama:height": meta["height"],
        },
        "assets": {
            "visual": {
                "href": f"{asset_base_url.rstrip('/')}/{rel}",
                "type": media_type,
                "roles": ["visual"],
                "title": path.name,
            }
        },
        "links": [],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="Folder to scan recursively")
    parser.add_argument("--dsn", required=True, help="Postgres DSN, e.g. postgresql://user:pass@localhost:5439/postgis")
    parser.add_argument("--asset-base-url", required=True, help="Base URL the files are served from, e.g. http://localhost:8081")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help=(
            "The folder the fileserver actually serves as its root (e.g. "
            "./data, matching DATA_DIR in docker-compose.yml). Asset URLs "
            "are built as {asset-base-url}/{path relative to this}. "
            "Defaults to `folder` itself if not given - only correct when "
            "you're scanning the fileserver's root directly rather than a "
            "subfolder of it."
        ),
    )
    parser.add_argument(
        "--pointcloud-collection",
        default=None,
        help=(
            "Force all point clouds into one collection with this id, "
            "regardless of folder. If not given (the default), each file "
            "gets its own collection named after the folder that directly "
            "contains it - e.g. files under .../Prio_1/ land in a "
            "'prio-1' collection, files under .../SiteB/ in 'siteb', etc."
        ),
    )
    parser.add_argument("--panorama-collection", default="panoramas")
    parser.add_argument(
        "--assume-crs",
        default=None,
        help=(
            "CRS to use for LAS/LAZ files whose embedded CRS is missing or "
            "empty (e.g. an empty WKT VLR - a known issue with some COPC "
            "export pipelines). Accepts anything pyproj understands, e.g. "
            "'EPSG:5682' for DB_REF / 3-degree Gauss-Kruger zone 2. Only "
            "applied when the file itself has nothing usable - a real "
            "embedded CRS always takes priority."
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(".stac_ingest_manifest.json"),
        help=(
            "Cache file tracking which files were already successfully "
            "ingested (by path + mtime + size), so re-running the script "
            "against the same top-level folder only re-parses files that "
            "are new or changed. Deleted if you want to force a full "
            "re-scan. Default: ./.stac_ingest_manifest.json"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Scan and report only, do not write to the database")
    args = parser.parse_args()

    assume_crs = CRS.from_user_input(args.assume_crs) if args.assume_crs else None

    root = args.folder.resolve()
    if not root.is_dir():
        sys.exit(f"{root} is not a directory")

    data_root = args.data_root.resolve() if args.data_root else root
    try:
        root.relative_to(data_root)
    except ValueError:
        sys.exit(
            f"--data-root {data_root} is not a parent of {root} - asset URLs would be "
            "wrong. Point --data-root at the folder the fileserver actually serves."
        )

    manifest = load_manifest(args.manifest)
    skipped_unchanged = 0

    las_items_by_collection: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_las = 0
    for path in iter_files(root, LAS_EXTENSIONS):
        rel = path.relative_to(data_root).as_posix()
        fingerprint = file_fingerprint(path)
        if manifest.get(rel) == fingerprint:
            skipped_unchanged += 1
            continue
        meta = read_las_metadata(path, assume_crs=assume_crs)
        if meta is None:
            skipped_las += 1
            continue
        collection_id = args.pointcloud_collection or sanitize_collection_id(path.parent.name)
        las_items_by_collection[collection_id].append(
            build_las_item(path, meta, collection_id, args.asset_base_url, data_root)
        )
        manifest[rel] = fingerprint

    pano_items: list[dict[str, Any]] = []
    skipped_pano = 0
    for path in iter_files(root, IMAGE_EXTENSIONS):
        rel = path.relative_to(data_root).as_posix()
        fingerprint = file_fingerprint(path)
        if manifest.get(rel) == fingerprint:
            skipped_unchanged += 1
            continue
        meta = read_pano_metadata(path)
        if meta is None:
            skipped_pano += 1
            continue
        pano_items.append(build_pano_item(path, meta, args.panorama_collection, args.asset_base_url, data_root))
        manifest[rel] = fingerprint

    if skipped_unchanged:
        log.info("Skipped %d file(s) unchanged since the last successful ingest", skipped_unchanged)

    total_las = sum(len(items) for items in las_items_by_collection.values())
    log.info(
        "Found %d valid point cloud(s) across %d collection(s) (%d skipped) and %d valid panorama(s) (%d skipped)",
        total_las, len(las_items_by_collection), skipped_las, len(pano_items), skipped_pano,
    )

    if args.dry_run:
        log.info("Dry run - nothing written to the database")
        return

    if not las_items_by_collection and not pano_items:
        log.info("Nothing to load")
        return

    db = PgstacDB(dsn=args.dsn)
    loader = Loader(db=db)

    for collection_id, items in las_items_by_collection.items():
        collection_doc = base_collection(
            collection_id,
            f"LAS/LAZ point clouds ingested from local storage (folder: {collection_id})",
        )
        bbox, time_range = batch_extent(items)
        existing = fetch_existing_extent(args.dsn, collection_id)
        collection_doc["extent"] = merge_extents(existing, bbox, time_range)
        loader.load_collections([collection_doc], insert_mode=Methods.upsert)
        loader.load_items(items, insert_mode=Methods.upsert)

    if pano_items:
        collection_doc = base_collection(
            args.panorama_collection,
            "360 panoramic photos ingested from local storage",
        )
        bbox, time_range = batch_extent(pano_items)
        existing = fetch_existing_extent(args.dsn, args.panorama_collection)
        collection_doc["extent"] = merge_extents(existing, bbox, time_range)
        loader.load_collections([collection_doc], insert_mode=Methods.upsert)
        loader.load_items(pano_items, insert_mode=Methods.upsert)

    log.info("Loaded %d point cloud item(s) and %d panorama item(s) into pgstac", total_las, len(pano_items))

    # Only persist the manifest once the DB writes above have actually
    # succeeded - if anything raised before this point, we want the
    # affected files retried on the next run rather than skipped forever.
    save_manifest(args.manifest, manifest)


if __name__ == "__main__":
    main()
