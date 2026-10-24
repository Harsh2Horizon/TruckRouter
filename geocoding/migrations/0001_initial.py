import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="FuelLocation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("source", models.CharField(default="opis", max_length=50)),
                ("external_id", models.CharField(max_length=100)),
                ("address", models.JSONField()),
                ("aliases", models.JSONField(default=list)),
                ("warnings", models.JSONField(default=list)),
                ("resolution_status", models.CharField(default="unmatched", max_length=20)),
                ("resolved_coordinate", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("source", "external_id"), name="unique_input_location"
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="SourcePlace",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("provider", models.CharField(max_length=30)),
                ("namespace", models.CharField(max_length=150)),
                ("source_id", models.CharField(max_length=300)),
                ("release", models.CharField(blank=True, max_length=100)),
                ("name_key", models.CharField(blank=True, max_length=500)),
                ("brand_key", models.CharField(blank=True, db_index=True, max_length=500)),
                ("store_id", models.CharField(blank=True, db_index=True, max_length=300)),
                ("country", models.CharField(blank=True, max_length=2)),
                ("state", models.CharField(blank=True, max_length=100)),
                ("city_key", models.CharField(blank=True, db_index=True, max_length=150)),
                ("address", models.JSONField()),
                (
                    "latitude",
                    models.FloatField(
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(-90),
                            django.core.validators.MaxValueValidator(90),
                        ],
                    ),
                ),
                (
                    "longitude",
                    models.FloatField(
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(-180),
                            django.core.validators.MaxValueValidator(180),
                        ],
                    ),
                ),
                ("precision", models.CharField(default="unknown", max_length=30)),
                ("operating_status", models.CharField(default="unknown", max_length=30)),
                ("existence_confidence", models.FloatField(null=True)),
                ("provenance", models.JSONField(default=list)),
                ("license", models.TextField(blank=True)),
                ("raw", models.JSONField()),
                ("imported_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["country", "state", "city_key"],
                        name="place_country_state_city_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("provider", "namespace", "source_id"), name="unique_provider_place"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(latitude__isnull=True, longitude__isnull=True),
                            models.Q(
                                latitude__isnull=False,
                                longitude__isnull=False,
                                latitude__gte=-90,
                                latitude__lte=90,
                                longitude__gte=-180,
                                longitude__lte=180,
                            ),
                            _connector="OR",
                        ),
                        name="valid_coordinate_pair",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(existence_confidence__isnull=True),
                            models.Q(existence_confidence__gte=0, existence_confidence__lte=1),
                            _connector="OR",
                        ),
                        name="valid_existence_confidence",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="FuelObservation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("dataset_sha256", models.CharField(max_length=64)),
                ("row_number", models.PositiveIntegerField()),
                ("raw", models.JSONField()),
                ("retail_price", models.DecimalField(decimal_places=8, max_digits=16, null=True)),
                ("price_unit", models.CharField(default="unknown", max_length=30)),
                ("currency", models.CharField(blank=True, max_length=3)),
                ("observed_at", models.DateTimeField(null=True)),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="observations",
                        to="geocoding.fuellocation",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("dataset_sha256", "row_number"), name="unique_input_row"
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="GeocodingRun",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("query", models.JSONField()),
                ("algorithm_version", models.CharField(max_length=30)),
                ("status", models.CharField(max_length=20)),
                ("result", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "location",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="geocoding.fuellocation",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="MatchCandidate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("score", models.FloatField()),
                ("evidence", models.JSONField()),
                ("snapshot", models.JSONField()),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="candidates",
                        to="geocoding.geocodingrun",
                    ),
                ),
                (
                    "source_place",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="geocoding.sourceplace",
                    ),
                ),
            ],
        ),
    ]
