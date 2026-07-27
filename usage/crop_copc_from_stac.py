#!/usr/bin/env python3
"""
Crop a small area out of a (possibly huge) COPC point cloud, without
downloading the whole file, and save the result as both .npz and .laz.

Entry point is the STAC API (standardized item-search), not the file
directly:

  1. GET {stac-api}/search?collections=...&bbox=... to find the item(s)
     covering the area you want (STAC API - Item Search, core spec).
  2. Read the "data" asset href off the matching item - that's the actual
     COPC file location.
  3. Open it with laspy's CopcReader, which does real HTTP range-request
     partial reads: only the octree nodes intersecting your bounds are
     fetched and decompressed, never the whole file.
  4. Save the result twice:
       - {output-dir}/{name}.npz  - x/y/z (+ intensity/classification/etc)
         as plain numpy arrays, for quick loading in analysis code.
       - {output-dir}/{name}.laz  - a real LAZ file with a proper header,
         correct point format, and the CRS embedded (via header.add_crs),
         so it opens correctly in QGIS/CloudCompare/etc.

Usage:
    python crop_copc_from_stac.py \
        --stac-api http://localhost:8080 \
        --collection pointclouds \
        --bbox 7.077 50.736 7.079 50.738 \
        --crs EPSG:5682 \
        --name crop \
        --output-dir output

`--bbox` is WGS84 lon/lat (min_lon min_lat max_lon max_lat), matching
STAC's own bbox convention - same as what you'd draw in QGIS's STAC
filter dialog.

`--crs` is the point cloud's *native* CRS (the one the file's actual
X/Y/Z coordinates are stored in). This has to be supplied explicitly for
now, since STAC items don't carry it yet - see the note at the bottom of
this file about adding a `proj:code` property during ingest to remove
this requirement.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import requests
from laspy import Bounds, CopcReader, LasData, LasHeader
from pyproj import CRS, Transformer


def find_item(stac_api: str, collection: str, bbox: list[float], item_id: str | None) -> dict:
    if item_id:
        resp = requests.get(f"{stac_api.rstrip('/')}/collections/{collection}/items/{item_id}")
        resp.raise_for_status()
        return resp.json()

    resp = requests.get(
        f"{stac_api.rstrip('/')}/search",
        params={"collections": collection, "bbox": ",".join(map(str, bbox)), "limit": 1},
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        sys.exit(f"No items in collection {collection!r} intersect bbox {bbox}")
    return features[0]


def native_bounds(bbox_wgs84: list[float], native_crs: str) -> Bounds:
    transformer = Transformer.from_crs("EPSG:4326", native_crs, always_xy=True)
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    corners = [
        transformer.transform(min_lon, min_lat),
        transformer.transform(max_lon, min_lat),
        transformer.transform(max_lon, max_lat),
        transformer.transform(min_lon, max_lat),
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return Bounds(mins=np.array([min(xs), min(ys)]), maxs=np.array([max(xs), max(ys)]))


def save_npz(points, out_path: Path) -> list[str]:
    save_kwargs = {"x": np.asarray(points.x), "y": np.asarray(points.y), "z": np.asarray(points.z)}
    for extra_dim in ("intensity", "classification", "return_number", "gps_time"):
        if extra_dim in points.point_format.dimension_names:
            save_kwargs[extra_dim] = np.asarray(points[extra_dim])
    np.savez(out_path, **save_kwargs)
    return list(save_kwargs.keys())


def save_laz(points, reader: CopcReader, native_crs: str, out_path: Path) -> None:
    # Match the source file's point format and raw scale/offset exactly -
    # the points already came from that encoding, so nothing needs
    # rescaling. version 1.4 is required for COPC-style PDRFs (6/7/8).
    header = LasHeader(point_format=reader.header.point_format, version="1.4")
    header.scales = reader.header.scales
    header.offsets = reader.header.offsets
    header.add_crs(CRS.from_user_input(native_crs))

    las = LasData(header=header, points=points)
    las.write(str(out_path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stac-api", default="http://localhost:8080", help="STAC API root URL")
    parser.add_argument("--collection", required=True, help="Collection ID, e.g. pointclouds")
    parser.add_argument("--item", default=None, help="Specific item ID (skips the bbox search)")
    parser.add_argument(
        "--bbox", type=float, nargs=4, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        required=True, help="Crop area in WGS84 lon/lat - also used to find the item if --item is not given",
    )
    parser.add_argument(
        "--crs", required=True,
        help="The point cloud's native CRS (e.g. EPSG:5682) - needed to convert --bbox into the file's own coordinates",
    )
    parser.add_argument("--resolution", type=float, default=None, help="Optional: limit octree levels fetched, for a coarser/faster crop")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Folder for output files (created if missing)")
    parser.add_argument("--name", default="crop", help="Base filename (without extension) for both outputs")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    npz_path = args.output_dir / f"{args.name}.npz"
    laz_path = args.output_dir / f"{args.name}.laz"

    item = find_item(args.stac_api, args.collection, args.bbox, args.item)
    href = item["assets"]["data"]["href"]
    print(f"Using item {item['id']!r}, asset: {href}")

    bounds = native_bounds(args.bbox, args.crs)
    print(f"Querying native bounds: mins={bounds.mins}, maxs={bounds.maxs}")

    with CopcReader.open(href) as reader:
        points = reader.query(bounds=bounds, resolution=args.resolution)
        print(f"Got {len(points)} points (out of the full file - only the matching chunks were fetched)")

        keys = save_npz(points, npz_path)
        print(f"Saved {npz_path}: {keys}")

        save_laz(points, reader, args.crs, laz_path)
        print(f"Saved {laz_path} (CRS: {args.crs}, point format: {reader.header.point_format.id})")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Note: making --crs unnecessary
#
# Right now the script needs --crs because STAC items in this pipeline only
# store the WGS84-reprojected bbox/geometry, not the point cloud's native
# CRS. The STAC "proj" extension has a standard field for exactly this -
# `proj:code` (e.g. "EPSG:5682") - which would let this script read the
# native CRS straight off the item instead of you having to know and pass
# it. Worth adding to scan_and_ingest.py's build_las_item() as a follow-up:
# one extra property, and every downstream tool (including this script)
# stops needing --crs at all.
# ---------------------------------------------------------------------------
