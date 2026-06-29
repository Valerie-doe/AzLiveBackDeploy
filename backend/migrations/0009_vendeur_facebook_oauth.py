from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('backend', '0008_produit_photo_to_imagefield'),
    ]

    operations = [
        migrations.AddField(
            model_name='vendeur',
            name='facebook_access_token',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='vendeur',
            name='facebook_user_id',
            field=models.CharField(blank=True, max_length=255, null=True, unique=True),
        ),
    ]
