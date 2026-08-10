import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Count, Max


def initialise_topic_history_and_deduplicate_keywords(apps, schema_editor):
    Topic = apps.get_model("topics", "Topic")
    TopicKeyword = apps.get_model("topics", "TopicKeyword")
    Topic.objects.filter(last_seen_run__isnull=True).update(
        last_seen_run=models.F("first_seen_run")
    )
    duplicate_groups = (
        TopicKeyword.objects.values("topic_id", "keyword")
        .annotate(keep_id=Max("id"), row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    for group in duplicate_groups.iterator():
        TopicKeyword.objects.filter(
            topic_id=group["topic_id"], keyword=group["keyword"]
        ).exclude(id=group["keep_id"]).delete()


class Migration(migrations.Migration):
    dependencies = [("runs", "0002_alter_run_settings_snapshot"), ("topics", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="topic",
            name="last_seen_run",
            field=models.ForeignKey(
                blank=True,
                help_text="Most recent pipeline run in which this stable topic was produced.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="topics_last_seen",
                to="runs.run",
            ),
        ),
        migrations.RunPython(
            initialise_topic_history_and_deduplicate_keywords,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="topickeyword",
            constraint=models.UniqueConstraint(
                fields=("topic", "keyword"), name="unique_topic_keyword"
            ),
        ),
    ]
