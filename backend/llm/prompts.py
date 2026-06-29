"""Prompts (instructions) envoyés au LLM.

Optimisés pour du texte MIXTE malgache + français : glossaire explicite,
normalisation des couleurs/tailles, exemples (few-shot), et — pour les
commentaires — injection du catalogue produits pour que le LLM choisisse
directement la bonne variante.
"""

COMMENT_SYSTEM = (
    "Tu es l'assistant d'une vente en direct (live shopping) a Madagascar. "
    "Les messages melangent souvent le malgache et le francais dans la meme phrase. "
    "Tu comprends parfaitement le malgache courant et l'argot des lives. "
    "Tu reponds STRICTEMENT en JSON valide, sans aucun texte autour."
)

CONFIRMATION_SYSTEM = (
    "Tu es l'assistant qui traite les reponses privees des clients pour finaliser "
    "leurs commandes (live shopping a Madagascar). Les messages melangent malgache "
    "et francais. Tu comprends parfaitement le malgache courant. "
    "Tu reponds STRICTEMENT en JSON valide, sans aucun texte autour."
)


# Glossaire malgache reutilise par les deux taches
_GLOSSAIRE = (
    "GLOSSAIRE MALGACHE -> FRANCAIS:\n"
    "- Intention d'ACHAT: maka, haka, alaiko, alako, alaina, mividy, te haka, te-haka, "
    "te hividy, mila, mangataka, JP, je prends, je veux, command. Variante 'ie/eny' apres "
    "une question = oui.\n"
    "- ANNULATION/refus: tsy maka, tsy haka, tsy mila, tsy te, tsy alaiko, tsia, aza, "
    "ajanona, foano, hadino, je ne prends plus, annuler, non merci.\n"
    "- COULEURS (rends-les en francais canonique): mena=rouge, manga=bleu, mainty=noir, "
    "fotsy=blanc, maitso=vert, mavo=jaune, volomparasy=violet, volontany=marron, "
    "volom-boasary=orange, rose=rose, gris=gris.\n"
    "- TAILLES: S, M, L, XL, XXL, XS. 'lehibe'/'be'=grand (L/XL), 'kely'=petit (S/M), "
    "'salantsalany'=moyen (M).\n"
    "- QUANTITE: iray=1, roa=2, telo=3, efatra=4, dimy=5, enina=6, fito=7, valo=8.\n"
    "- LIVRAISON: anarana=nom, finday/numero/laharana=telephone, adiresy/toerana/lalana=adresse.\n"
    "- DATES relatives: anio=aujourd'hui, rahampitso=demain, afaka ampitso=apres-demain, "
    "herinandro=semaine, alatsinainy=lundi, talata=mardi, alarobia=mercredi, alakamisy=jeudi, "
    "zoma=vendredi, sabotsy=samedi, alahady=dimanche.\n"
    "- HEURE: 'amin'ny X ora'=X heures; 'maraina'=matin, 'tolakandro/hariva'=apres-midi/soir.\n"
)


def build_comment_prompt(comment_text: str, catalog_lines: list[str] | None = None) -> str:
    catalog_block = "\n".join(catalog_lines) if catalog_lines else "(catalogue indisponible)"

    schema = (
        "{\n"
        '  "intention": "achat" | "inconnu",\n'
        '  "variante_id": number | null,   // DOIT provenir du CATALOGUE ci-dessous\n'
        '  "code_jp": string | null,       // code JP de la variante choisie\n'
        '  "produit_id": number | null,\n'
        '  "product_query": string,        // ce que le client demande (nom ou code), en MAJUSCULES\n'
        '  "couleur": string | null,       // en francais canonique (rouge, bleu...)\n'
        '  "taille": string | null,        // S, M, L, XL, XXL, XS\n'
        '  "quantite": number | null\n'
        "}\n"
    )

    examples = (
        'Commentaire: "JP1 mena taille M, maka aho" '
        '-> {"intention":"achat","product_query":"JP1","couleur":"rouge","taille":"M","quantite":1}\n'
        'Commentaire: "alaiko ny robe mainty kely" '
        '-> {"intention":"achat","product_query":"ROBE","couleur":"noir","taille":"S","quantite":1}\n'
        'Commentaire: "mbola misy ve ny manga?" '
        '-> {"intention":"inconnu","product_query":"MANGA","couleur":"bleu"}\n'
        'Commentaire: "tsara be io e" '
        '-> {"intention":"inconnu","product_query":""}\n'
    )

    return (
        "Analyse ce commentaire de live (malgache et/ou francais).\n\n"
        + _GLOSSAIRE
        + "\nCATALOGUE DISPONIBLE (choisis l'article correspondant; variante_id et code_jp "
        "DOIVENT venir de cette liste, sinon null):\n"
        + catalog_block
        + "\n\nRenvoie UNIQUEMENT un JSON avec EXACTEMENT ces cles:\n"
        + schema
        + "\nRegles:\n"
        "- intention='achat' s'il y a une volonte de commander/reserver (cf. glossaire), "
        "meme implicite. Sinon 'inconnu'.\n"
        "- Choisis dans le CATALOGUE la variante la plus probable (par code JP, nom de produit, "
        "couleur, taille). Si aucune ne correspond, variante_id=null et code_jp=null.\n"
        "- Traduis toujours la couleur en francais canonique.\n\n"
        "EXEMPLES:\n"
        + examples
        + '\nCommentaire: "'
        + comment_text
        + '"'
    )


def build_confirmation_prompt(
    message_text: str,
    client_context: dict | None = None,
    today_iso: str | None = None,
) -> str:
    context_line = ''
    if client_context:
        context_line = (
            "\nInfos deja connues du client (a completer, ne pas inventer): "
            + str(client_context)
            + "\n"
        )
    today_line = ("\nDate du jour (pour resoudre les dates relatives): " + today_iso + "\n") if today_iso else ""

    schema = (
        "{\n"
        '  "intention": "annulation" | "confirmation" | "autre",\n'
        '  "nom": string | null,\n'
        '  "telephone": string | null,        // format malgache: 0 + 9 chiffres (0XXXXXXXXX)\n'
        '  "adresse": string | null,\n'
        '  "date_livraison": string | null,   // format YYYY-MM-DD\n'
        '  "heure_livraison": string | null   // format HH:MM sur 24h\n'
        "}\n"
    )

    examples = (
        'Message: "Marie 0341234567 Toamasina rahampitso 2 ora" (si anio=2026-06-24) '
        '-> {"intention":"confirmation","nom":"Marie","telephone":"0341234567",'
        '"adresse":"Toamasina","date_livraison":"2026-06-25","heure_livraison":"14:00"}\n'
        'Message: "tsy maka intsony aho" '
        '-> {"intention":"annulation"}\n'
        'Message: "Lova, finday 0329876543" '
        '-> {"intention":"confirmation","nom":"Lova","telephone":"0329876543"}\n'
    )

    return (
        "Analyse la reponse privee d'un client pour finaliser sa commande "
        "(malgache et/ou francais).\n\n"
        + _GLOSSAIRE
        + today_line
        + context_line
        + "\nRenvoie UNIQUEMENT un JSON avec EXACTEMENT ces cles:\n"
        + schema
        + "\nRegles:\n"
        "- 'annulation' si refus/annulation (cf. glossaire).\n"
        "- 'confirmation' s'il fournit des infos de livraison (nom, tel, adresse, date, heure).\n"
        "- Sinon 'autre'.\n"
        "- Normalise: telephone=0XXXXXXXXX, date=YYYY-MM-DD (resous anio/rahampitso/jours), "
        "heure=HH:MM. null si absent.\n\n"
        "EXEMPLES:\n"
        + examples
        + '\nMessage: "'
        + message_text
        + '"'
    )
