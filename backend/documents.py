import io
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .jp_codes import code_for_commande, format_jp_code
from .models import Commande


def _commande_variante(commande: Commande):
    if commande.variante_id:
        return commande.variante
    return commande.produit.variantes.order_by('id').first()


def _format_price(value) -> str:
    return f'{int(value):,} Ar'.replace(',', ' ')


def _draw_hline(pdf: canvas.Canvas, x1: float, x2: float, y: float, stroke: float = 0.6):
    pdf.setStrokeColorRGB(0.75, 0.75, 0.75)
    pdf.setLineWidth(stroke)
    pdf.line(x1, y, x2, y)
    pdf.setStrokeColorRGB(0, 0, 0)


def build_facture_pdf(commande: Commande) -> bytes:
    variante = _commande_variante(commande)
    client = commande.client
    produit = commande.produit
    vendeur = produit.vendeur
    prix = variante.prix_unitaire if variante else commande.get_prix_unitaire()
    quantite = commande.quantite_effective
    total = prix * quantite
    code_jp = format_jp_code(code_for_commande(commande)) if code_for_commande(commande) else '—'

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left = 40
    right = width - 40
    y = height - 36

    # En-tête marque
    pdf.setFillColorRGB(0.08, 0.12, 0.2)
    pdf.rect(0, height - 72, width, 72, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont('Helvetica-Bold', 20)
    pdf.drawString(left, height - 38, 'AZLive')
    pdf.setFont('Helvetica', 10)
    pdf.drawString(left, height - 54, 'Facture de commande live')
    pdf.setFont('Helvetica-Bold', 12)
    pdf.drawRightString(right, height - 38, f'#{commande.id}')
    pdf.setFont('Helvetica', 9)
    emis = datetime.now().strftime('%d/%m/%Y %H:%M')
    pdf.drawRightString(right, height - 54, f'Émise le {emis}')
    pdf.setFillColorRGB(0, 0, 0)

    y = height - 100
    pdf.setFont('Helvetica-Bold', 11)
    pdf.drawString(left, y, 'Vendeur')
    pdf.drawString(width / 2, y, 'Client')
    y -= 16
    pdf.setFont('Helvetica', 10)
    vendeur_lines = [
        vendeur.nom if vendeur else '—',
        f'Contact : {vendeur.contact}' if vendeur and vendeur.contact else 'Contact : —',
    ]
    client_lines = [
        client.nom or '—',
        f'Tél : {client.telephone or "—"}',
        f'Adresse : {client.adresse or "—"}',
    ]
    block_top = y
    for line in vendeur_lines:
        pdf.drawString(left, y, line[:48])
        y -= 14
    y_client = block_top
    for line in client_lines:
        pdf.drawString(width / 2, y_client, line[:48])
        y_client -= 14
    y = min(y, y_client) - 8

    _draw_hline(pdf, left, right, y)
    y -= 22

    # Livraison
    pdf.setFont('Helvetica-Bold', 11)
    pdf.drawString(left, y, 'Livraison')
    y -= 16
    pdf.setFont('Helvetica', 10)
    date_liv = client.date_livraison_preferee.strftime('%d/%m/%Y') if client.date_livraison_preferee else '—'
    heure_liv = (
        client.heure_livraison_preferee.strftime('%H:%M') if client.heure_livraison_preferee else '—'
    )
    pdf.drawString(left, y, f'Date souhaitée : {date_liv}    Heure : {heure_liv}')
    y -= 14
    pdf.drawString(left, y, f'Statut commande : {commande.get_statut_display()}')
    y -= 20
    _draw_hline(pdf, left, right, y)
    y -= 24

    # Tableau articles
    pdf.setFont('Helvetica-Bold', 11)
    pdf.drawString(left, y, 'Détail')
    y -= 8
    _draw_hline(pdf, left, right, y, stroke=1)
    y -= 16

    col_desc = left
    col_qty = left + 280
    col_pu = left + 340
    col_total = right

    pdf.setFont('Helvetica-Bold', 9)
    pdf.drawString(col_desc, y, 'Désignation')
    pdf.drawRightString(col_qty + 40, y, 'Qté')
    pdf.drawRightString(col_pu + 50, y, 'P.U.')
    pdf.drawRightString(col_total, y, 'Total')
    y -= 6
    _draw_hline(pdf, left, right, y)
    y -= 16

    designation = produit.nom
    if variante:
        designation += f' — {variante.couleur} / {variante.taille}'
    designation += f'  ({code_jp})'

    pdf.setFont('Helvetica', 10)
    pdf.drawString(col_desc, y, designation[:58])
    pdf.drawRightString(col_qty + 40, y, str(quantite))
    pdf.drawRightString(col_pu + 50, y, _format_price(prix))
    pdf.drawRightString(col_total, y, _format_price(total))
    y -= 14
    pdf.setFont('Helvetica', 8)
    pdf.setFillColorRGB(0.35, 0.35, 0.35)
    pdf.drawString(col_desc, y, f'Ordre JP #{commande.ordre_jp}')
    pdf.setFillColorRGB(0, 0, 0)
    y -= 18
    _draw_hline(pdf, left, right, y)
    y -= 28

    # Totaux
    pdf.setFont('Helvetica', 10)
    pdf.drawRightString(right - 120, y, 'Sous-total')
    pdf.drawRightString(right, y, _format_price(total))
    y -= 16
    pdf.setFont('Helvetica-Bold', 13)
    pdf.drawRightString(right - 120, y, 'TOTAL À PAYER')
    pdf.drawRightString(right, y, _format_price(total))
    y -= 28

    pdf.setFont('Helvetica', 9)
    pdf.setFillColorRGB(0.25, 0.25, 0.25)
    pdf.drawString(left, y, 'Mode de paiement : à la livraison (sauf indication contraire).')
    y -= 14
    pdf.drawString(left, y, 'Merci pour votre confiance — à bientôt sur le prochain live.')
    pdf.setFillColorRGB(0, 0, 0)

    # Pied de page
    pdf.setFont('Helvetica', 8)
    pdf.setFillColorRGB(0.45, 0.45, 0.45)
    base = getattr(settings, 'AZLIVE_PUBLIC_BASE_URL', '') or ''
    footer = f'AZLive Madagascar  •  Commande #{commande.id}'
    if base:
        footer += f'  •  {base.rstrip("/")}/api/commandes/{commande.id}/facture.pdf'
    pdf.drawCentredString(width / 2, 28, footer)
    pdf.setFillColorRGB(0, 0, 0)

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.read()


def build_etiquette_livraison_pdf(commande: Commande) -> bytes:
    """Ticket livreur : compact, lisible, infos essentielles pour la tournée."""
    variante = _commande_variante(commande)
    client = commande.client
    produit = commande.produit
    prix = variante.prix_unitaire if variante else commande.get_prix_unitaire()
    quantite = commande.quantite_effective
    total = prix * quantite
    bare_code = code_for_commande(commande)
    code_jp = format_jp_code(bare_code) if bare_code else f'CMD-{commande.id}'

    date_liv = (
        client.date_livraison_preferee.strftime('%d/%m/%Y')
        if client.date_livraison_preferee
        else '—'
    )
    heure_liv = (
        client.heure_livraison_preferee.strftime('%H:%M')
        if client.heure_livraison_preferee
        else ''
    )
    creneau = f'{date_liv} {heure_liv}'.strip() if heure_liv else date_liv

    variante_bits = []
    if variante:
        if variante.couleur:
            variante_bits.append(variante.couleur)
        if variante.taille:
            variante_bits.append(variante.taille)
    variante_label = ' / '.join(variante_bits)

    buffer = io.BytesIO()
    # Format ticket thermique approx. 80×100 mm (lisible pour le livreur).
    page_width = 80 * mm
    page_height = 100 * mm
    pdf = canvas.Canvas(buffer, pagesize=(page_width, page_height))
    margin = 4 * mm
    content_w = page_width - 2 * margin

    # Cadre
    pdf.setStrokeColorRGB(0.15, 0.15, 0.15)
    pdf.setLineWidth(1.2)
    pdf.rect(2 * mm, 2 * mm, page_width - 4 * mm, page_height - 4 * mm, stroke=1, fill=0)

    y = page_height - 8 * mm

    # Bandeau haut
    pdf.setFillColorRGB(0.08, 0.12, 0.2)
    pdf.rect(2 * mm, page_height - 14 * mm, page_width - 4 * mm, 12 * mm, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont('Helvetica-Bold', 11)
    pdf.drawString(margin, page_height - 9.5 * mm, 'AZLive  ·  LIVRAISON')
    pdf.setFont('Helvetica-Bold', 12)
    pdf.drawRightString(page_width - margin, page_height - 9.5 * mm, f'#{commande.id}')
    pdf.setFillColorRGB(0, 0, 0)

    y = page_height - 20 * mm

    # Code JP très visible
    pdf.setFont('Helvetica-Bold', 16)
    pdf.drawCentredString(page_width / 2, y, code_jp)
    y -= 5 * mm
    pdf.setStrokeColorRGB(0.7, 0.7, 0.7)
    pdf.setLineWidth(0.5)
    pdf.line(margin, y, page_width - margin, y)
    y -= 6 * mm

    # Client
    pdf.setFont('Helvetica-Bold', 8)
    pdf.setFillColorRGB(0.35, 0.35, 0.35)
    pdf.drawString(margin, y, 'CLIENT')
    pdf.setFillColorRGB(0, 0, 0)
    y -= 5 * mm
    pdf.setFont('Helvetica-Bold', 12)
    pdf.drawString(margin, y, (client.nom or '—')[:28])
    y -= 5 * mm
    pdf.setFont('Helvetica-Bold', 11)
    pdf.drawString(margin, y, client.telephone or '—')
    y -= 5 * mm
    pdf.setFont('Helvetica', 9)
    adresse = (client.adresse or '—').strip()
    # Wrap adresse sur 2 lignes max
    max_chars = 36
    if len(adresse) <= max_chars:
        pdf.drawString(margin, y, adresse)
        y -= 5 * mm
    else:
        pdf.drawString(margin, y, adresse[:max_chars])
        y -= 4 * mm
        pdf.drawString(margin, y, adresse[max_chars : max_chars * 2])
        y -= 5 * mm

    pdf.setStrokeColorRGB(0.7, 0.7, 0.7)
    pdf.line(margin, y, page_width - margin, y)
    y -= 6 * mm

    # Produit
    pdf.setFont('Helvetica-Bold', 8)
    pdf.setFillColorRGB(0.35, 0.35, 0.35)
    pdf.drawString(margin, y, 'PRODUIT')
    pdf.setFillColorRGB(0, 0, 0)
    y -= 5 * mm
    pdf.setFont('Helvetica-Bold', 10)
    pdf.drawString(margin, y, (produit.nom or '—')[:32])
    y -= 4.5 * mm
    pdf.setFont('Helvetica', 9)
    detail = f'Qté : {quantite}'
    if variante_label:
        detail += f'   ·   {variante_label}'
    pdf.drawString(margin, y, detail[:40])
    y -= 6 * mm

    pdf.setStrokeColorRGB(0.7, 0.7, 0.7)
    pdf.line(margin, y, page_width - margin, y)
    y -= 6 * mm

    # Créneau + montant COD
    pdf.setFont('Helvetica-Bold', 8)
    pdf.setFillColorRGB(0.35, 0.35, 0.35)
    pdf.drawString(margin, y, 'LIVRAISON')
    pdf.setFillColorRGB(0, 0, 0)
    y -= 5 * mm
    pdf.setFont('Helvetica-Bold', 11)
    pdf.drawString(margin, y, creneau)
    y -= 7 * mm

    pdf.setFillColorRGB(0.08, 0.12, 0.2)
    pdf.roundRect(margin, y - 2 * mm, content_w, 10 * mm, 2 * mm, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont('Helvetica-Bold', 9)
    pdf.drawString(margin + 2 * mm, y + 2.5 * mm, 'À ENCAISSER')
    pdf.setFont('Helvetica-Bold', 12)
    pdf.drawRightString(page_width - margin - 2 * mm, y + 2 * mm, _format_price(total))
    pdf.setFillColorRGB(0, 0, 0)

    # Pied
    pdf.setFont('Helvetica', 7)
    pdf.setFillColorRGB(0.4, 0.4, 0.4)
    pdf.drawCentredString(page_width / 2, 4.5 * mm, f'Paiement à la livraison  ·  JP #{commande.ordre_jp}')
    pdf.setFillColorRGB(0, 0, 0)

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.read()


def pdf_response(pdf_bytes: bytes, filename: str) -> HttpResponse:
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response
