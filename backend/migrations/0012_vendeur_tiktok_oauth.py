from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('backend', '0011_live_diffusion_plateformes'),
        ('backend', '0010_produitimage'),
    ]

    operations = [
        migrations.AddField(
            model_name='vendeur',
            name='tiktok_access_token',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='vendeur',
            name='tiktok_open_id',
            field=models.CharField(blank=True, max_length=255, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='vendeur',
            name='tiktok_refresh_token',
            field=models.TextField(blank=True, null=True),
        ),
    ]
