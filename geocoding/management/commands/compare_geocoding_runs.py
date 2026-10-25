"""Explain coverage changes using the actual old provider records and new gates."""

import csv
import json
from collections import Counter
from pathlib import Path
from statistics import median

from django.core.management.base import BaseCommand

from geocoding.acquisition import write_json
from geocoding.exports import PROVIDERS
from geocoding.locality_enrichment import original_locality_address
from geocoding.matching import score_addresses
from geocoding.models import SourcePlace
from geocoding.parsing import parse_address
from geocoding.schemas import CanonicalAddress


def read_rows(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def has_point(row, provider):
    return bool(row[f"{provider}_latitude"] and row[f"{provider}_longitude"])


class Command(BaseCommand):
    help = "Compare v2/v3 coordinate availability and explain rejected old POI candidates"

    def add_arguments(self, parser):
        parser.add_argument("--old-run", default="output/geocoding-v2")
        parser.add_argument("--new-run", default="output/geocoding-v3")
        parser.add_argument("--output-dir", default="output/geocoding-coverage-analysis")

    def handle(self, *args, **options):
        old_dir, new_dir, output = (Path(options[k]) for k in ("old_run", "new_run", "output_dir"))
        old_report = json.loads((old_dir / "report.json").read_text())
        new_report = json.loads((new_dir / "report.json").read_text())
        output.mkdir(parents=True, exist_ok=True)
        old = {r["external_id"]: r for r in read_rows(old_dir / "geocodes.csv")}
        new = {r["external_id"]: r for r in read_rows(new_dir / "geocodes.csv")}
        assert old.keys() == new.keys()
        old_source = [json.loads(line) for line in (old_dir / "per-source.jsonl").open()]
        old_index = {(r["external_id"], r["provider"]): r for r in old_source}
        addresses = {}
        for provider in PROVIDERS:
            ids = {
                r["matched_source"]["source_id"]
                for r in old_source
                if r["provider"] == provider and r.get("matched_source")
            }
            for place in (
                SourcePlace.objects.filter(provider=provider, source_id__in=ids)
                .values("provider", "namespace", "source_id", "address", "provenance")
                .iterator()
            ):
                addresses[(provider, place["namespace"], place["source_id"])] = (
                    place["address"]
                    if old_report.get("locality_enrichment")
                    else original_locality_address(place["address"], place["provenance"])
                )
        report = {
            "old_run": str(old_dir),
            "new_run": str(new_dir),
            "unique_stops": len(old),
            "providers": {},
            "stop_transitions": {},
            "row_counts": {},
        }
        report["same_source_releases_and_record_counts"] = all(
            old_report["sources"][provider][key] == new_report["sources"][provider][key]
            for provider in PROVIDERS
            for key in ("release", "unique_stored_records")
        )
        audit = []
        stop_causes = Counter()
        for provider in PROVIDERS:
            lost = [
                k for k in old if has_point(old[k], provider) and not has_point(new[k], provider)
            ]
            gained = [
                k for k in old if not has_point(old[k], provider) and has_point(new[k], provider)
            ]
            failures = Counter()
            scores, statuses = [], Counter()
            for key in old:
                previous = old_index[(key, provider)]
                if previous.get("matched_coordinate"):
                    scores.append(previous["score"])
                    statuses[previous["status"]] += 1
            for key in lost:
                row = new[key]
                previous = old_index[(key, provider)]
                identity = previous["matched_source"]
                address = CanonicalAddress.model_validate(
                    addresses[(provider, identity["namespace"], identity["source_id"])]
                )
                query = parse_address(
                    {
                        "name": row["name"],
                        "address": row["address"],
                        "city": row["city"],
                        "state": row["state"],
                        "country": row["country"],
                    }
                )
                evidence = score_addresses(query, address)
                causes = [f"missing_{field}" for field in evidence.locality["missing"]]
                if not evidence.locality["missing"] and not evidence.locality["complete"]:
                    if evidence.locality["city"] < 0.90:
                        causes.append("city_below_90_percent")
                    if not evidence.locality["state"]:
                        causes.append("state_conflict")
                    if not evidence.locality["country"]:
                        causes.append("country_conflict")
                if evidence.components["name"] < 0.70:
                    causes.append("name_below_70_percent")
                if not causes:
                    causes.append("other_retrieval_or_eligibility_difference")
                failures.update(causes)
                newly_empty = not any(has_point(row, p) for p in PROVIDERS)
                if newly_empty:
                    stop_causes.update(causes)
                audit.append(
                    {
                        "external_id": key,
                        "name": row["name"],
                        "city": row["city"],
                        "state": row["state"],
                        "provider": provider,
                        "old_source_name": address.raw_name,
                        "old_source_city": address.city,
                        "old_source_state": address.state,
                        "old_source_country": address.country,
                        "old_score": previous["score"],
                        "old_status": previous["status"],
                        "old_latitude": old[key][f"{provider}_latitude"],
                        "old_longitude": old[key][f"{provider}_longitude"],
                        "new_name_similarity": evidence.components["name"],
                        "new_city_similarity": evidence.locality["city"],
                        "causes": ";".join(causes),
                        "all_three_now_empty": newly_empty,
                        "fallback_method": row["prediction_method"],
                        "fallback_reason": row["fallback_reason"],
                    }
                )
            report["providers"][provider] = {
                "old_with_coordinate": sum(has_point(r, provider) for r in old.values()),
                "new_with_coordinate": sum(has_point(r, provider) for r in new.values()),
                "lost": len(lost),
                "gained": len(gained),
                "rejection_causes": dict(failures),
                "old_located_candidate_median_score": median(scores),
                "old_located_candidate_statuses": dict(statuses),
            }
        old_empty = {k for k, r in old.items() if not any(has_point(r, p) for p in PROVIDERS)}
        new_empty = {k for k, r in new.items() if not any(has_point(r, p) for p in PROVIDERS)}
        report["stop_transitions"] = {
            "old_all_three_empty": len(old_empty),
            "new_all_three_empty": len(new_empty),
            "newly_all_three_empty": len(new_empty - old_empty),
            "newly_recovered_provider_point": len(old_empty - new_empty),
            "empty_in_both": len(old_empty & new_empty),
            "new_final_prediction_empty": sum(not r["predicted_latitude"] for r in new.values()),
            "exit_fallback_recovered": sum(
                r["prediction_method"] == "highway_exit" for r in new.values()
            ),
        }
        for label, directory in (("old", old_dir), ("new", new_dir)):
            rows = read_rows(directory / "fuel-prices-geocoded.csv")
            report["row_counts"][label] = {
                "rows": len(rows),
                "all_three_empty": sum(not any(has_point(r, p) for p in PROVIDERS) for r in rows),
            }
        with (output / "lost-provider-candidates.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(audit[0]))
            writer.writeheader()
            writer.writerows(audit)
        write_json(output / "comparison.json", report)
        self.stdout.write(json.dumps(report, indent=2))
