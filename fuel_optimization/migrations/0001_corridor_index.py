"""Add PostGIS/index support without changing existing geocoding model contracts."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("geocoding", "0001_initial")]
    operations = [
        # Preserve the shared extension on rollback; only our index is owned here.
        migrations.RunSQL("CREATE EXTENSION IF NOT EXISTS postgis", migrations.RunSQL.noop),
        migrations.RunSQL(
            """CREATE INDEX fuel_location_accepted_geog_idx
               ON geocoding_fuellocation USING gist (
                 (ST_SetSRID(ST_MakePoint(
                   (resolved_coordinate->>'longitude')::double precision,
                   (resolved_coordinate->>'latitude')::double precision),4326)::geography))
               WHERE resolution_status='accepted' AND resolved_coordinate IS NOT NULL""",
            "DROP INDEX IF EXISTS fuel_location_accepted_geog_idx",
        ),
    ]
