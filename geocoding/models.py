import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q


class FuelLocation(models.Model):
    """Input identity, not an assertion that a geocode has been verified."""

    source = models.CharField(max_length=50, default="opis")
    external_id = models.CharField(max_length=100)
    address = models.JSONField()
    aliases = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    resolution_status = models.CharField(max_length=20, default="unmatched")
    resolved_coordinate = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "external_id"], name="unique_input_location")
        ]


class FuelObservation(models.Model):
    """Retain every source row, including duplicates and source price precision."""

    location = models.ForeignKey(
        FuelLocation, on_delete=models.CASCADE, related_name="observations"
    )
    dataset_sha256 = models.CharField(max_length=64)
    row_number = models.PositiveIntegerField()
    raw = models.JSONField()
    retail_price = models.DecimalField(max_digits=16, decimal_places=8, null=True)
    price_unit = models.CharField(max_length=30, default="unknown")
    currency = models.CharField(max_length=3, blank=True)
    observed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dataset_sha256", "row_number"], name="unique_input_row"
            )
        ]


class SourcePlace(models.Model):
    """Latest imported provider record; runs snapshot the evidence used."""

    provider = models.CharField(max_length=30)
    namespace = models.CharField(max_length=150)
    source_id = models.CharField(max_length=300)
    release = models.CharField(max_length=100, blank=True)
    name_key = models.CharField(max_length=500, blank=True)
    brand_key = models.CharField(max_length=500, blank=True, db_index=True)
    store_id = models.CharField(max_length=300, blank=True, db_index=True)
    country = models.CharField(max_length=2, blank=True)
    state = models.CharField(max_length=100, blank=True)
    city_key = models.CharField(max_length=150, blank=True, db_index=True)
    address = models.JSONField()
    latitude = models.FloatField(
        null=True, validators=[MinValueValidator(-90), MaxValueValidator(90)]
    )
    longitude = models.FloatField(
        null=True, validators=[MinValueValidator(-180), MaxValueValidator(180)]
    )
    precision = models.CharField(max_length=30, default="unknown")
    operating_status = models.CharField(max_length=30, default="unknown")
    existence_confidence = models.FloatField(null=True)
    provenance = models.JSONField(default=list)
    license = models.TextField(blank=True)
    raw = models.JSONField()
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["country", "state", "city_key"], name="place_country_state_city_idx"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "namespace", "source_id"], name="unique_provider_place"
            ),
            models.CheckConstraint(
                condition=(
                    Q(latitude__isnull=True, longitude__isnull=True)
                    | Q(
                        latitude__isnull=False,
                        longitude__isnull=False,
                        latitude__gte=-90,
                        latitude__lte=90,
                        longitude__gte=-180,
                        longitude__lte=180,
                    )
                ),
                name="valid_coordinate_pair",
            ),
            models.CheckConstraint(
                condition=Q(
                    Q(existence_confidence__isnull=True),
                    Q(existence_confidence__gte=0, existence_confidence__lte=1),
                    _connector="OR",
                ),
                name="valid_existence_confidence",
            ),
        ]


class GeocodingRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    location = models.ForeignKey(FuelLocation, null=True, on_delete=models.SET_NULL)
    query = models.JSONField()
    algorithm_version = models.CharField(max_length=30)
    status = models.CharField(max_length=20)
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class MatchCandidate(models.Model):
    run = models.ForeignKey(GeocodingRun, on_delete=models.CASCADE, related_name="candidates")
    source_place = models.ForeignKey(SourcePlace, null=True, on_delete=models.SET_NULL)
    score = models.FloatField()
    evidence = models.JSONField()
    snapshot = models.JSONField()
