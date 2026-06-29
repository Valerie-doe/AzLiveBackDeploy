from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('backend', '0009_vendeur_facebook_oauth'),
    ]

    operations = [
        migrations.AddField(
            model_name='pagefacebook',
            name='webhook_subscribed',
            field=models.BooleanField(default=False),
        ),
    ]
