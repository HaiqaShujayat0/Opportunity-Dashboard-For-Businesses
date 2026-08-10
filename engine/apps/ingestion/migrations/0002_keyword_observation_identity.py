from django.db import migrations, models
from django.db.models import Count, Max


IDENTITY_FIELDS = (
    "run_id",
    "market_id",
    "keyword_normalised",
    "source",
    "signal",
    "competitor_domain",
)


def remove_duplicate_observations(apps, schema_editor):
    Observation = apps.get_model("ingestion", "KeywordObservation")
    duplicate_groups = (
        Observation.objects.values(*IDENTITY_FIELDS)
        .annotate(keep_id=Max("id"), row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    for group in duplicate_groups.iterator():
        identity = {field: group[field] for field in IDENTITY_FIELDS}
        Observation.objects.filter(**identity).exclude(id=group["keep_id"]).delete()


class Migration(migrations.Migration):
    dependencies = [("ingestion", "0001_initial")]

    operations = [
        migrations.RunPython(remove_duplicate_observations, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="keywordobservation",
            constraint=models.UniqueConstraint(
                fields=(
                    "run",
                    "market",
                    "keyword_normalised",
                    "source",
                    "signal",
                    "competitor_domain",
                ),
                name="unique_keyword_observation_identity",
            ),
        ),
    ]
