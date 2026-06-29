# Generated manually — refactor variant fields from Produit to Variante

from django.db import migrations, models


def migrate_produit_fields_to_variantes(apps, schema_editor):
    Produit = apps.get_model('backend', 'Produit')
    Variante = apps.get_model('backend', 'Variante')

    for produit in Produit.objects.all():
        variantes = list(Variante.objects.filter(produit_id=produit.id).order_by('id'))
        legacy_code_jp = getattr(produit, 'code_jp', None) or f'JP{produit.id}'
        legacy_prix = getattr(produit, 'prix', 0)
        legacy_taille = getattr(produit, 'taille', 'Freesize')
        legacy_couleur = getattr(produit, 'couleur', 'Unique')
        legacy_stock = getattr(produit, 'stock', 0)

        if not variantes:
            Variante.objects.create(
                produit_id=produit.id,
                taille=legacy_taille,
                couleur=legacy_couleur,
                prix_unitaire=legacy_prix,
                stock=legacy_stock,
                code_jp=legacy_code_jp,
            )
            continue

        for index, variante in enumerate(variantes):
            variante.prix_unitaire = legacy_prix
            if not variante.stock:
                variante.stock = legacy_stock
            variante.code_jp = legacy_code_jp if index == 0 else f'{legacy_code_jp}V{variante.id}'
            variante.save()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('backend', '0008_produit_photo_to_imagefield'),
    ]

    operations = [
        migrations.AddField(
            model_name='variante',
            name='prix_unitaire',
            field=models.DecimalField(decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='variante',
            name='code_jp',
            field=models.CharField(max_length=50, null=True),
        ),
        migrations.RunPython(migrate_produit_fields_to_variantes, noop_reverse),
        migrations.AlterField(
            model_name='variante',
            name='prix_unitaire',
            field=models.DecimalField(decimal_places=2, max_digits=10),
        ),
        migrations.AlterField(
            model_name='variante',
            name='code_jp',
            field=models.CharField(max_length=50, unique=True),
        ),
        migrations.AddConstraint(
            model_name='variante',
            constraint=models.UniqueConstraint(
                fields=('produit', 'taille', 'couleur'),
                name='unique_produit_taille_couleur',
            ),
        ),
        migrations.RemoveField(
            model_name='produit',
            name='code_jp',
        ),
        migrations.RemoveField(
            model_name='produit',
            name='couleur',
        ),
        migrations.RemoveField(
            model_name='produit',
            name='prix',
        ),
        migrations.RemoveField(
            model_name='produit',
            name='stock',
        ),
        migrations.RemoveField(
            model_name='produit',
            name='taille',
        ),
    ]
