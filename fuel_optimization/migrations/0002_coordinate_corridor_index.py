"""Index the current corridor policy: every location with a stored coordinate."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("fuel_optimization", "0001_corridor_index")]
    operations = [
        migrations.RunSQL(
            """CREATE INDEX fuel_location_geog_idx
               ON geocoding_fuellocation USING gist (
                 (ST_SetSRID(ST_MakePoint(
                   (resolved_coordinate->>'longitude')::double precision,
                   (resolved_coordinate->>'latitude')::double precision),4326)::geography))
               WHERE resolved_coordinate IS NOT NULL""",
            "DROP INDEX IF EXISTS fuel_location_geog_idx",
        ),
    ]
