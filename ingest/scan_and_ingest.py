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
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

import laspy
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
    # Extent is a wide-open placeholder. pgstac can recompute the real
    # extent from ingested items - see the README for how to do that.
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


def iter_files(root: Path, extensions: set[str]) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in extensions:
            yield path


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
    }


def build_las_item(path: Path, meta: dict[str, Any], collection: str, asset_base_url: str, root: Path) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    media_type = "application/vnd.laszip+copc" if meta["is_copc"] else "application/vnd.laszip"
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": path.stem,
        "collection": collection,
        "geometry": meta["geometry"],
        "bbox": meta["bbox"],
        "properties": {
            "datetime": meta["datetime"].isoformat(),
            "pc:count": meta["point_count"],
            "crs:assumed": meta["crs_assumed"],
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

    if height == 0 or abs(width / height - 2.0) > 0.05:
        log.warning(
            "Skipping %s: not a 2:1 equirectangular panorama (dimensions %sx%s)",
            path, width, height,
        )
        return None

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

    return {"lon": lon, "lat": lat, "datetime": capture_dt, "width": width, "height": height}


def build_pano_item(path: Path, meta: dict[str, Any], collection: str, asset_base_url: str, root: Path) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
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
            "panorama:type": "equirectangular",
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
    parser.add_argument("--pointcloud-collection", default="pointclouds")
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
    parser.add_argument("--dry-run", action="store_true", help="Scan and report only, do not write to the database")
    args = parser.parse_args()

    assume_crs = CRS.from_user_input(args.assume_crs) if args.assume_crs else None

    root = args.folder.resolve()
    if not root.is_dir():
        sys.exit(f"{root} is not a directory")

    las_items: list[dict[str, Any]] = []
    skipped_las = 0
    for path in iter_files(root, LAS_EXTENSIONS):
        meta = read_las_metadata(path, assume_crs=assume_crs)
        if meta is None:
            skipped_las += 1
            continue
        las_items.append(build_las_item(path, meta, args.pointcloud_collection, args.asset_base_url, root))

    pano_items: list[dict[str, Any]] = []
    skipped_pano = 0
    for path in iter_files(root, IMAGE_EXTENSIONS):
        meta = read_pano_metadata(path)
        if meta is None:
            skipped_pano += 1
            continue
        pano_items.append(build_pano_item(path, meta, args.panorama_collection, args.asset_base_url, root))

    log.info(
        "Found %d valid point cloud(s) (%d skipped) and %d valid panorama(s) (%d skipped)",
        len(las_items), skipped_las, len(pano_items), skipped_pano,
    )

    if args.dry_run:
        log.info("Dry run - nothing written to the database")
        return

    if not las_items and not pano_items:
        log.info("Nothing to load")
        return

    db = PgstacDB(dsn=args.dsn)
    loader = Loader(db=db)

    loader.load_collections(
        [base_collection(args.pointcloud_collection, "LAS/LAZ point clouds ingested from local storage")],
        insert_mode=Methods.upsert,
    )
    loader.load_collections(
        [base_collection(args.panorama_collection, "360 panoramic photos ingested from local storage")],
        insert_mode=Methods.upsert,
    )

    if las_items:
        loader.load_items(las_items, insert_mode=Methods.upsert)
    if pano_items:
        loader.load_items(pano_items, insert_mode=Methods.upsert)

    log.info("Loaded %d point cloud item(s) and %d panorama item(s) into pgstac", len(las_items), len(pano_items))


if __name__ == "__main__":
    main()
