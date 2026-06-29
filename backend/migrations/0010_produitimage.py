from django.db import migrations, models
import django.db.models.deletion


def migrate_legacy_photos(apps, schema_editor):
    Produit = apps.get_model('backend', 'Produit')
    ProduitImage = apps.get_model('backend', 'ProduitImage')

    for produit in Produit.objects.all():
        if produit.photo:
            ProduitImage.objects.create(produit=produit, image=str(produit.photo))


class Migration(migrations.Migration):

    dependencies = [
        ('backend', '0009_move_variant_fields_from_produit'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProduitImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.ImageField(upload_to='produits/')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('produit', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='images', to='backend.produit')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.RunPython(migrate_legacy_photos, migrations.RunPython.noop),
    ]
