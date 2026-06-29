"""Intégration LLM (Google Gemini) — module additif et isolé.

Ce package ajoute une couche d'analyse par LLM SANS modifier la logique métier
existante. Il expose des endpoints de test et réutilise, en repli (fallback),
les analyseurs regex déjà présents (`backend.ai`, `backend.order_confirmation`).
"""
