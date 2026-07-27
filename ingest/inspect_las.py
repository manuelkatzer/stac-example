#!/usr/bin/env python3
"""
Dump everything relevant to CRS detection from a single LAS/LAZ/COPC file:
version, point format, global encoding flags, and every VLR/EVLR (with
special attention to anything tagged LASF_Projection, which is where CRS
data lives).

Usage:
    python inspect_las.py path/to/segment_1.copc.laz
"""
import sys
from pathlib import Path

import laspy


def describe_vlr(vlr) -> str:
    user_id = getattr(vlr, "user_id", "?")
    record_id = getattr(vlr, "record_id", "?")
    description = getattr(vlr, "description", "")
    try:
        data_len = len(vlr.record_data_bytes())
    except Exception:  # noqa: BLE001 - some VLR types (e.g. COPC info) don't support this
        data_len = "?"
    return f"user_id={user_id!r:20} record_id={record_id!r:6} data_len={data_len!s:8} description={description!r}"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"Usage: python {Path(__file__).name} path/to/file.laz")

    path = Path(sys.argv[1])

    with laspy.open(path) as reader:
        header = reader.header

        print(f"File:            {path}")
        print(f"LAS version:     {header.major_version}.{header.minor_version}")
        print(f"Point format:    {header.point_format.id}")
        print(f"Point count:     {header.point_count}")
        print(f"Global encoding: {header.global_encoding!r}")
        try:
            print(f"  -> WKT bit set: {header.global_encoding.wkt}")
        except AttributeError:
            print(f"  -> raw value: {header.global_encoding.value!r}")
        print(f"Mins:            {header.mins}")
        print(f"Maxs:            {header.maxs}")

        print(f"\nRegular VLRs ({len(header.vlrs)}):")
        for vlr in header.vlrs:
            print(f"  {describe_vlr(vlr)}")

        print("\nForcing EVLR read...")
        evlrs = reader.evlrs
        print(f"EVLRs ({len(evlrs)}):")
        for evlr in evlrs:
            print(f"  {describe_vlr(evlr)}")

        print("\nparse_crs() before considering EVLRs manually:")
        print(f"  {header.parse_crs()!r}")

        print("\nLooking for LASF_Projection records specifically:")
        projection_records = list(header.vlrs.get_by_id("LASF_Projection")) + list(evlrs.get_by_id("LASF_Projection"))
        if not projection_records:
            print("  None found in either VLRs or EVLRs.")
        for rec in projection_records:
            print(f"  Found: {describe_vlr(rec)}")
            if hasattr(rec, "string"):
                print(f"    WKT string (first 300 chars): {rec.string[:300]!r}")
            if hasattr(rec, "parse_crs"):
                try:
                    print(f"    parse_crs() on this record: {rec.parse_crs()!r}")
                except Exception as exc:  # noqa: BLE001
                    print(f"    parse_crs() raised: {exc!r}")


if __name__ == "__main__":
    main()
