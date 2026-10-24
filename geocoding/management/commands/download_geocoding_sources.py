import csv
import fcntl
import hashlib
import json
import os
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from geocoding.acquisition import (
    FastDownloadClient,
    acquire_atp,
    acquire_osm,
    acquire_overture,
    write_json,
)
from geocoding.downloads import NORTH_AMERICA_BBOX, parse_bbox
from geocoding.parsing import parse_address


class Command(BaseCommand):
    help = "Download all three providers concurrently, with per-file resumption and manifests"

    def add_arguments(self, parser):
        parser.add_argument("--csv", default="fuel-prices-for-be-assessment.csv")
        parser.add_argument("--data-dir", default="data/sources")
        parser.add_argument("--output-dir", default="output/acquisition")
        parser.add_argument("--overture-bbox", type=parse_bbox, default=NORTH_AMERICA_BBOX)
        parser.add_argument("--overture-workers", type=int, default=2)
        parser.add_argument("--overture-mode", choices=["bulk", "remote"], default="bulk")
        parser.add_argument("--osm-workers", type=int, default=6)
        parser.add_argument(
            "--sources",
            nargs="+",
            choices=["alltheplaces", "overture", "osm"],
            default=["alltheplaces", "overture", "osm"],
        )
        parser.add_argument("--atp-spiders-file")

    def handle(self, *args, **options):
        output = Path(options["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        data_dir = Path(options["data_dir"])
        data_dir.mkdir(parents=True, exist_ok=True)
        self._run_lock = (data_dir / ".acquisition.lock").open("a+")
        try:
            fcntl.flock(self._run_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CommandError(
                "Another source download is running for this data directory"
            ) from exc
        if not 1 <= options["overture_workers"] <= 4 or not 1 <= options["osm_workers"] <= 8:
            raise CommandError("Use 1–4 Overture query workers and 1–8 OSM transfer workers")
        brands, regions, rows = set(), set(), 0
        with open(options["csv"], newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                address = parse_address(
                    {
                        "name": row["Truckstop Name"],
                        "address": row["Address"],
                        "city": row["City"],
                        "state": row["State"],
                    }
                )
                brands.add(address.brand)
                if address.country not in {"US", "CA"} or not address.state:
                    raise CommandError(f"Input row has unknown country/state: {rows + 2}")
                regions.add(f"{address.country}-{address.state}")
                rows += 1
        spiders = None
        if options["atp_spiders_file"]:
            spiders = json.loads(Path(options["atp_spiders_file"]).read_text())
        client = FastDownloadClient(
            options["data_dir"],
            os.getenv("GEOCODER_USER_AGENT", "truckroute/0.1 local research pipeline"),
        )
        # Install once before starting independent DuckDB connections.
        import duckdb

        extensions = (client.directory / "duckdb-extensions").resolve()
        extensions.mkdir(exist_ok=True)
        with duckdb.connect(config={"extension_directory": str(extensions)}) as connection:
            connection.execute("INSTALL spatial; INSTALL httpfs;")
        events = queue.Queue()
        report = {
            "started_at": datetime.now(UTC).isoformat(),
            "pid": os.getpid(),
            "status": "running",
            "input_rows": rows,
            "input_file": str(Path(options["csv"]).resolve()),
            "dataset_sha256": hashlib.sha256(Path(options["csv"]).read_bytes()).hexdigest(),
            "brands": len(brands),
            "regions": sorted(regions),
            "sources": {},
        }

        def progress(source):
            return lambda message: events.put((source, message))

        loaders = {
            "alltheplaces": lambda: acquire_atp(
                client, brands, progress("alltheplaces"), spiders=spiders
            ),
            "overture": lambda: acquire_overture(
                client,
                brands,
                progress("overture"),
                bbox=options["overture_bbox"],
                workers=options["overture_workers"],
                mode=options["overture_mode"],
            ),
            "osm": lambda: acquire_osm(
                client, regions, progress("osm"), workers=options["osm_workers"]
            ),
        }
        started = time.monotonic()
        last_transfer_update = started
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {source: pool.submit(loaders[source]) for source in options["sources"]}
            pending = set(futures)
            for source in pending:
                report["sources"][source] = {"status": "downloading"}
            while pending:
                try:
                    source, message = events.get(timeout=2)
                    report["sources"][source]["progress"] = message
                    self.stdout.write(f"[{source}] {message}")
                    self.stdout.flush()
                except queue.Empty:
                    pass
                for source in list(pending):
                    if futures[source].done():
                        try:
                            manifest = futures[source].result()
                            status = (
                                "downloaded"
                                if manifest.get("acquisition_complete", True)
                                else "partial"
                            )
                            report["sources"][source] = {"status": status, **manifest}
                            self.stdout.write(
                                f"[{source}] {status}: {manifest['records']:,} records"
                            )
                        except Exception as exc:
                            report["sources"][source] = {"status": "failed", "error": str(exc)}
                            self.stderr.write(f"[{source}] failed: {exc}")
                        self.stdout.flush()
                        pending.remove(source)
                report["updated_at"] = datetime.now(UTC).isoformat()
                report["elapsed_seconds"] = round(time.monotonic() - started, 2)
                report["active_transfers"] = client.transfer_progress()
                if time.monotonic() - last_transfer_update >= 15 and report["active_transfers"]:
                    transfers = report["active_transfers"]
                    received = sum(row["received_bytes"] for row in transfers)
                    expected = sum(row["expected_bytes"] or 0 for row in transfers)
                    self.stdout.write(
                        f"[downloads] {len(transfers)} active files: "
                        f"{received / 1e6:.1f}/{expected / 1e6:.1f} MB received"
                    )
                    self.stdout.flush()
                    last_transfer_update = time.monotonic()
                write_json(output / "report.json", report)
        report["finished_at"] = datetime.now(UTC).isoformat()
        complete = all(row["status"] == "downloaded" for row in report["sources"].values())
        report["status"] = "completed" if complete else "incomplete"
        write_json(output / "report.json", report)
        if not complete:
            raise CommandError(f"Acquisition incomplete; see {output}/report.json")
        self.stdout.write(f"Downloaded all requested sources in {report['elapsed_seconds']}s")
