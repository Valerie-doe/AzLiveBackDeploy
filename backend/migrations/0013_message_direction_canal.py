from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('backend', '0012_vendeur_tiktok_oauth'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='canal',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
        migrations.AddField(
            model_name='message',
            name='direction',
            field=models.CharField(
                choices=[('outbound', 'Sortant'), ('inbound', 'Entrant')],
                default='outbound',
                max_length=20,
            ),
        ),
    ]
