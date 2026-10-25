import hashlib
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from geocoding.acquisition import FastDownloadClient, write_json
from geocoding.geography import REFERENCE_DIR, build_reference, get_exit_locator


class Command(BaseCommand):
    help = "Cache city anchors, US state boundaries and verified US interstate exit references"

    def add_arguments(self, parser):
        parser.add_argument("--acquisition-report", default="output/acquisition/report.json")
        parser.add_argument("--directory", default=str(REFERENCE_DIR))
        parser.add_argument("--cached-only", action="store_true")

    def handle(self, *args, **options):
        directory = Path(options["directory"])
        client = FastDownloadClient(directory)
        if not options["cached_only"]:
            release = client.json(
                "https://api.github.com/repos/tldev/openinterstate/releases/latest"
            )
            asset = next(a for a in release["assets"] if a["name"].endswith(".tar.gz"))
            digest = asset.get("digest") or ""
            archive = client.fetch(
                asset["browser_download_url"],
                expected_bytes=asset["size"],
                sha256=digest.removeprefix("sha256:") if digest else None,
            )
            checksums = next((a for a in release["assets"] if a["name"] == "checksums.txt"), None)
            expected = {}
            if checksums:
                for line in (
                    client.fetch(checksums["browser_download_url"]).read_text().splitlines()
                ):
                    parts = line.split(maxsplit=1)
                    if len(parts) == 2:
                        expected[parts[1].lstrip("*")] = parts[0]
            with tarfile.open(archive) as package:
                member = next(
                    m for m in package.getmembers() if m.name.endswith("corridor_exits.csv")
                )
                payload = package.extractfile(member).read()
                checksum = hashlib.sha256(payload).hexdigest()
                published = next(
                    (
                        value
                        for name, value in expected.items()
                        if name.endswith("corridor_exits.csv")
                    ),
                    None,
                )
                if published and published != checksum:
                    raise CommandError("Exit reference checksum mismatch")
                (directory / "interstate-exits.csv").write_bytes(payload)
            write_json(
                directory / "interstate-release.json",
                {
                    "release": release["tag_name"],
                    "url": asset["browser_download_url"],
                    "bytes": asset["size"],
                    "filename": str(directory / "interstate-exits.csv"),
                    "license": "ODbL-1.0",
                    "exits_sha256": checksum,
                    "published_checksum_verified": bool(published),
                },
            )
            osm = json.loads(Path(options["acquisition_report"]).read_text())["sources"]["osm"]
            region_manifests = osm.get("region_manifests")
            if not region_manifests:
                raise CommandError("The acquisition report must include OSM regional manifests")

            def download(row):
                info = row["metadata"]
                stats = info["theme_stats"]["places"]
                url = row["url"] + "places.parquet"
                filename = client.fetch(
                    url, expected_bytes=stats["size_bytes"], sha256=stats["sha256"]
                )
                return {
                    "region": row["region"],
                    "filename": str(filename),
                    "url": url,
                    "sha256": stats["sha256"],
                    "source_timestamp": info["source_timestamp"],
                }

            with ThreadPoolExecutor(max_workers=6) as pool:
                regions = list(pool.map(download, region_manifests))
            write_json(
                directory / "places-manifest.json",
                {"snapshot": osm["snapshot"], "regions": regions},
            )
            states = client.json("https://www.geoboundaries.org/api/current/gbOpen/USA/ADM1/")
            filename = client.fetch(states["gjDownloadURL"])
            write_json(
                directory / "usa-states-manifest.json",
                {"filename": str(filename), "metadata": states},
            )
            (directory / "geographic-index.json").unlink(missing_ok=True)
        manifest = build_reference(directory)
        get_exit_locator.cache_clear()
        self.stdout.write(
            json.dumps(
                {
                    key: manifest[key]
                    for key in (
                        "exit_records",
                        "city_records",
                        "coverage",
                        "max_city_exit_distance_m",
                    )
                },
                indent=2,
            )
        )
