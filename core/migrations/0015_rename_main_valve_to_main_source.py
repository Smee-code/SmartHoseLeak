"""
Rename the recorded source from "Main valve" to "Main source".

It was named for the old rig, where one valve fed a single monitored run and
the distance markers were measured from it. Every household now has its own
hose leaving this point, so what matters about it is that it is the source,
not that there is a valve there - and the valves that do matter are the
motorised ones on each household's line, which would be confused with it.

Only the default name is changed. A site that has renamed it to something
real - "Spring box", "Reservoir tank" - is left alone.
"""

from django.db import migrations


def rename(apps, schema_editor):
    apps.get_model("core", "WaterSource").objects.filter(name="Main valve").update(
        name="Main source"
    )


def unrename(apps, schema_editor):
    apps.get_model("core", "WaterSource").objects.filter(name="Main source").update(
        name="Main valve"
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0014_valve_test_request")]
    operations = [migrations.RunPython(rename, unrename)]
