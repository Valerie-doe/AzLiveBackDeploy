import io
from datetime import datetime

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


def build_facture_pdf(commande: Commande) -> bytes:
    variante = _commande_variante(commande)
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 40

    pdf.setFont('Helvetica-Bold', 16)
    pdf.drawString(40, y, 'AZLive — Facture')
    y -= 24
    pdf.setFont('Helvetica', 10)
    pdf.drawString(40, y, f'Commande #{commande.id} — {datetime.now().strftime("%d/%m/%Y %H:%M")}')
    y -= 30

    pdf.setFont('Helvetica-Bold', 12)
    pdf.drawString(40, y, 'Client')
    y -= 18
    pdf.setFont('Helvetica', 11)
    for line in (
        f'Nom : {commande.client.nom}',
        f'Téléphone : {commande.client.telephone or "—"}',
        f'Adresse : {commande.client.adresse or "—"}',
        f'Date livraison souhaitée : {commande.client.date_livraison_preferee or "—"}',
        f'Heure souhaitée : {commande.client.heure_livraison_preferee.strftime("%H:%M") if commande.client.heure_livraison_preferee else "—"}',
    ):
        pdf.drawString(40, y, line)
        y -= 16

    y -= 10
    pdf.setFont('Helvetica-Bold', 12)
    pdf.drawString(40, y, 'Produit')
    y -= 18
    pdf.setFont('Helvetica', 11)
    prix = variante.prix_unitaire if variante else commande.get_prix_unitaire()
    quantite = commande.quantite_effective
    total = prix * quantite
    pdf.drawString(40, y, f'{commande.produit.nom}')
    y -= 16
    if variante:
        pdf.drawString(40, y, f'Taille {variante.taille} — Couleur {variante.couleur} — Code {format_jp_code(code_for_commande(commande))}')
        y -= 16
    pdf.drawString(40, y, f'Prix unitaire : {_format_price(prix)}')
    y -= 16
    pdf.drawString(40, y, f'Quantité : {quantite}')
    y -= 16
    pdf.drawString(40, y, f'Ordre JP : #{commande.ordre_jp}')
    y -= 24

    pdf.setFont('Helvetica-Bold', 14)
    pdf.drawString(40, y, f'TOTAL : {_format_price(total)}')
    y -= 30
    pdf.setFont('Helvetica-Oblique', 9)
    pdf.drawString(40, y, 'Merci pour votre achat live — AZLive Madagascar')

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.read()


def build_etiquette_livraison_pdf(commande: Commande) -> bytes:
    variante = _commande_variante(commande)
    buffer = io.BytesIO()
    page_width = 80 * mm
    page_height = 50 * mm
    pdf = canvas.Canvas(buffer, pagesize=(page_width, page_height))

    pdf.setFont('Helvetica-Bold', 11)
    pdf.drawCentredString(page_width / 2, page_height - 12 * mm, 'AZLive — ETIQUETTE LIVRAISON')

    pdf.setFont('Helvetica-Bold', 10)
    bare_code = code_for_commande(commande)
    code = format_jp_code(bare_code) if bare_code else f'CMD-{commande.id}'
    pdf.drawCentredString(page_width / 2, page_height - 20 * mm, code)

    pdf.setFont('Helvetica', 9)
    produit_line = commande.produit.nom.upper()
    if variante:
        produit_line += f' ({variante.couleur}, {variante.taille})'
    if commande.quantite_effective > 1:
        produit_line += f' x{commande.quantite_effective}'
    pdf.drawCentredString(page_width / 2, page_height - 27 * mm, produit_line[:40])

    pdf.setFont('Helvetica-Bold', 10)
    prix = variante.prix_unitaire if variante else commande.get_prix_unitaire()
    total = prix * commande.quantite_effective
    pdf.drawCentredString(page_width / 2, page_height - 34 * mm, _format_price(total))

    pdf.setFont('Helvetica', 8)
    pdf.drawCentredString(page_width / 2, page_height - 40 * mm, f'#{commande.id} — {commande.client.nom[:28]}')
    pdf.drawCentredString(page_width / 2, page_height - 45 * mm, commande.client.telephone or '—')
    delivery = commande.client.date_livraison_preferee.strftime('%d/%m/%Y') if commande.client.date_livraison_preferee else '—'
    if commande.client.heure_livraison_preferee:
        delivery += f' {commande.client.heure_livraison_preferee.strftime("%H:%M")}'
    pdf.drawCentredString(page_width / 2, page_height - 49 * mm, delivery[:32])

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.read()


def pdf_response(pdf_bytes: bytes, filename: str) -> HttpResponse:
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response
