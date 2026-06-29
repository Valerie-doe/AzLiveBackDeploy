from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('backend', '0010_pagefacebook_webhook_subscribed'),
    ]

    operations = [
        migrations.AddField(
            model_name='live',
            name='date_debut',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='live',
            name='date_fin',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='live',
            name='diffusion_plateformes',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='live',
            name='statut',
            field=models.CharField(
                choices=[('planifie', 'Planifié'), ('en_cours', 'En cours'), ('termine', 'Terminé')],
                default='planifie',
                max_length=50,
            ),
        ),
    ]
