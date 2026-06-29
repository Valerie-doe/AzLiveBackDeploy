from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('backend', '0013_message_direction_canal'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='heure_livraison_preferee',
            field=models.TimeField(blank=True, null=True),
        ),
    ]
