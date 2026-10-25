#!/usr/bin/env python3
"""Build, fetch, restore and verify the small, pinned reproducibility snapshot."""

import argparse
import gzip
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/reproducibility-snapshot.json"
MANIFEST = "data/replay/bundle-manifest.json"
TABLES = ("geocoding_fuellocation", "geocoding_fuelobservation", "geocoding_sourceplace")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def portable(value):
    """Metadata paths must resolve inside the recipient's checkout, not the author's home."""
    if isinstance(value, str):
        return value.removeprefix(str(ROOT) + "/")
    if isinstance(value, dict):
        return {key: portable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [portable(item) for item in value]
    return value


def encoded(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def build(archive, database_dump):
    acquisition = read(ROOT / "output/acquisition/report.json")
    imports = read(ROOT / "output/refactor-verification/poi/report.json")
    entries = {}

    def include(path):
        path = Path(path).resolve()
        relative = path.relative_to(ROOT).as_posix()
        if not path.is_file():
            raise ValueError(f"Missing snapshot input: {relative}")
        entries[relative] = encoded(portable(read(path))) if path.suffix == ".json" else path

    for provider in acquisition["sources"].values():
        include(provider["filename"])
    for name in (
        "geographic-index.json",
        "coverage/manifest.json",
        "localities/manifest.json",
        "usa-states-manifest.json",
        "places-manifest.json",
        "interstate-release.json",
    ):
        path = ROOT / "data/reference" / name
        include(path)
        for key, value in read(path).items():
            if key.endswith("_file") or key == "filename":
                include(value)
    census = ROOT / "data/reference/coverage/census-addresses.json"
    include(census)
    for result in read(census).values():
        if result.get("response_file"):
            include(result["response_file"])
    entries["data/replay/core-data.dump"] = Path(database_dump).resolve()
    entries["data/replay/acquisition-report.json"] = encoded(portable(acquisition))
    entries["data/replay/import-report.json"] = encoded(
        portable(
            {
                "status": "completed",
                "algorithm_version": imports["algorithm_version"],
                "input_import": imports["input_import"],
                "sources": imports["sources"],
            }
        )
    )
    entries["data/replay/locality-report.json"] = encoded(
        portable(read(ROOT / "output/locality-enrichment/report.json"))
    )
    for stage, filename in (
        ("poi", "geocodes.csv"),
        ("poi", "source-comparison.csv"),
        ("final", "all-records-geocoded.csv"),
    ):
        entries[f"data/replay/expected/{stage}/{filename}"] = (
            ROOT / "output/refactor-verification" / stage / filename
        )
    entries["data/replay/ATTRIBUTION.md"] = (
        b"# Snapshot data attribution\n\n"
        b"AllThePlaces: CC0; original spider and source URI retained.\n"
        b"OpenStreetMap contributors / OpenInterstate: ODbL 1.0.\n"
        b"GeoNames: CC BY 4.0. Overture: licenses and lineage retained per source record.\n"
        b"Census and geoBoundaries: original reference metadata retained in manifests.\n"
        b"The input fuel-price file is user supplied. The repository's code license does "
        b"not replace the original datasets' licenses.\n"
    )
    files = []
    for name, source in sorted(entries.items()):
        files.append(
            {
                "path": name,
                "bytes": len(source) if isinstance(source, bytes) else source.stat().st_size,
                "sha256": hashlib.sha256(source).hexdigest()
                if isinstance(source, bytes)
                else digest(source),
            }
        )
    metadata = {
        "format_version": 1,
        "snapshot_date": "2026-10-25",
        "input_sha256": imports["input_import"]["dataset_sha256"],
        "database_counts": dict(
            zip(
                TABLES,
                (
                    imports["parser"]["unique_ids"],
                    imports["input_import"]["rows"],
                    sum(s["unique_stored_records"] for s in imports["sources"].values()),
                ),
                strict=True,
            )
        ),
        "files": files,
    }
    archive = Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("wb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|") as package:
            for name, source in [(MANIFEST, encoded(metadata)), *sorted(entries.items())]:
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                info.size = len(source) if isinstance(source, bytes) else source.stat().st_size
                if isinstance(source, bytes):
                    package.addfile(info, io.BytesIO(source))
                else:
                    with source.open("rb") as content:
                        package.addfile(info, content)
    config = {
        "format_version": 1,
        "snapshot_date": metadata["snapshot_date"],
        "branch": "refactor/latest-geocoding-clean",
        "url": "https://github.com/Harsh2Horizon/TruckRouter/releases/download/"
        "data-snapshot-2026-10-25/" + archive.name,
        "bytes": archive.stat().st_size,
        "sha256": digest(archive),
        "database_counts": metadata["database_counts"],
        "source_releases": {p: r["release"] for p, r in imports["sources"].items()},
    }
    CONFIG.write_bytes(encoded(config))
    print(json.dumps(config, indent=2), flush=True)


def fetch(archive):
    config = read(CONFIG)
    archive = Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.is_file():
        partial = archive.with_suffix(archive.suffix + ".part")
        subprocess.run(
            [
                "curl",
                "--fail",
                "--location",
                "--retry",
                "3",
                "--continue-at",
                "-",
                "--output",
                str(partial),
                config["url"],
            ],
            check=True,
        )
        if partial.stat().st_size != config["bytes"] or digest(partial) != config["sha256"]:
            raise ValueError("Snapshot size/SHA256 verification failed")
        partial.replace(archive)
    if archive.stat().st_size != config["bytes"] or digest(archive) != config["sha256"]:
        raise ValueError("Snapshot size/SHA256 verification failed")
    with tarfile.open(archive, mode="r|gz") as package:
        members = iter(package)
        first = next(members)
        if first.name != MANIFEST or not first.isfile():
            raise ValueError("Invalid snapshot manifest")
        manifest_bytes = package.extractfile(first).read()
        metadata = json.loads(manifest_bytes)
        allowed = {entry["path"]: entry for entry in metadata["files"]}
        if len(allowed) != len(metadata["files"]):
            raise ValueError("Duplicate snapshot manifest entries")
        # Refuse to overwrite a different local dataset or reference cache.
        for name, entry in allowed.items():
            if not name.startswith("data/") or ".." in PurePosixPath(name).parts:
                raise ValueError(f"Unsafe snapshot path: {name}")
            target = ROOT / name
            if not target.resolve().is_relative_to(ROOT):
                raise ValueError(f"Snapshot path escapes the checkout: {name}")
            if target.exists() and (not target.is_file() or digest(target) != entry["sha256"]):
                raise ValueError(f"Existing snapshot input differs: {name}. Use a fresh checkout.")
        found = set()
        for member in members:
            if (
                member.name not in allowed
                or not member.isfile()
                or not member.name.startswith("data/")
            ):
                raise ValueError(f"Unexpected snapshot member: {member.name}")
            if member.name in found or member.size != allowed[member.name]["bytes"]:
                raise ValueError("Duplicate or incorrectly sized snapshot member")
            package.extract(member, path=ROOT, filter="data")
            if digest(ROOT / member.name) != allowed[member.name]["sha256"]:
                raise ValueError(f"Extracted file failed SHA256 check: {member.name}")
            found.add(member.name)
        if found != set(allowed):
            raise ValueError("Snapshot archive is incomplete")
    target = ROOT / MANIFEST
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(manifest_bytes)
    print(f"Verified and extracted {len(found)} files", flush=True)


def restore():
    import django

    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "truckroute.settings")
    django.setup()
    from django.conf import settings
    from django.db import connection

    metadata = read(ROOT / MANIFEST)
    expected = metadata["database_counts"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM geocoding_fuellocation UNION ALL "
            "SELECT count(*) FROM geocoding_fuelobservation UNION ALL "
            "SELECT count(*) FROM geocoding_sourceplace"
        )
        counts = dict(zip(TABLES, (r[0] for r in cursor.fetchall()), strict=True))
    dump = ROOT / "data/replay/core-data.dump"
    checksum = next(r["sha256"] for r in metadata["files"] if r["path"].endswith("core-data.dump"))
    if digest(dump) != checksum:
        raise ValueError("Database snapshot checksum mismatch")
    marker = ROOT / "data/replay/restore-state.json"
    database = settings.DATABASES["default"]
    identity = {"name": database["NAME"], "host": database["HOST"], "port": database["PORT"]}
    if counts == expected and marker.exists() and read(marker) == {**identity, "sha256": checksum}:
        print("Reusing the already restored core snapshot")
        return
    if any(counts.values()):
        raise ValueError(
            "Core tables are not empty. Restore into an empty database; no rows were deleted."
        )
    with dump.open("rb") as stream:
        subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--data-only",
                "--single-transaction",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--username",
                database["USER"],
                "--dbname",
                database["NAME"],
            ],
            stdin=stream,
            check=True,
        )
    with connection.cursor() as cursor:
        for table, count in expected.items():
            cursor.execute(f"SELECT count(*) FROM {table}")
            if cursor.fetchone()[0] != count:
                raise ValueError(f"Restored row count mismatch: {table}")
    marker.write_bytes(encoded({**identity, "sha256": checksum}))
    print("Restored and checked the frozen location, observation and POI tables", flush=True)


def extensions():
    import duckdb

    directory = (ROOT / "data/sources/duckdb-extensions").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(config={"extension_directory": str(directory)}) as db:
        db.execute("INSTALL spatial; INSTALL httpfs;")
    print("DuckDB extensions ready for this operating system/architecture", flush=True)


def verify(run):
    import csv

    mismatches = []
    checked = {}
    for stage, filename in (
        ("poi", "geocodes.csv"),
        ("poi", "source-comparison.csv"),
        ("final", "all-records-geocoded.csv"),
    ):
        with (ROOT / "data/replay/expected" / stage / filename).open(newline="") as stream:
            expected = list(csv.DictReader(stream))
        with (Path(run) / stage / filename).open(newline="") as stream:
            actual = list(csv.DictReader(stream))
        if len(expected) != len(actual):
            mismatches.append({"file": filename, "reason": "different row count"})
        for number, (left, right) in enumerate(zip(expected, actual, strict=False), start=2):
            for field in left.keys() | right.keys():
                if field == "run_id" or left.get(field) == right.get(field):
                    continue
                numeric = any(
                    token in field.lower()
                    for token in (
                        "latitude",
                        "longitude",
                        "distance",
                        "score",
                        "similarity",
                        "points",
                        "margin",
                        "deviation_m",
                        "coverage",
                        "applicable_weight",
                    )
                )
                if numeric:
                    try:
                        if math.isclose(
                            float(left[field]), float(right[field]), abs_tol=1e-7, rel_tol=1e-9
                        ):
                            continue
                    except (ValueError, TypeError, KeyError):
                        pass
                mismatches.append(
                    {
                        "file": filename,
                        "row": number,
                        "field": field,
                        "expected": left.get(field),
                        "actual": right.get(field),
                    }
                )
        checked[f"{stage}/{filename}"] = len(actual)
    report = {
        "passed": not mismatches,
        "rows_checked": checked,
        "differences": len(mismatches),
        "examples": mismatches[:20],
    }
    (Path(run) / "snapshot-verification.json").write_bytes(encoded(report))
    print(json.dumps(report, indent=2), flush=True)
    if mismatches:
        raise ValueError(
            "Outputs differ from the published snapshot; inspect snapshot-verification.json"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--archive", required=True)
    build_parser.add_argument("--database-dump", required=True)
    fetch_parser = sub.add_parser("fetch")
    fetch_parser.add_argument("--archive", default="output/snapshot-cache/truckrouter-data.tar.gz")
    sub.add_parser("restore")
    sub.add_parser("extensions")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--run", default="output/reproduced")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.command == "build":
        build(args.archive, args.database_dump)
    elif args.command == "fetch":
        fetch(args.archive)
    elif args.command == "restore":
        restore()
    elif args.command == "extensions":
        extensions()
    else:
        verify(args.run)


if __name__ == "__main__":
    main()
