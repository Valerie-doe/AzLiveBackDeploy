# Déployer MediaMTX sur Railway (Option A — live Facebook réel)

Objectif : même flux qu’en local.

```text
Navigateur (HTTPS) → WHIP → MediaMTX (Railway)
                              ↓ RTSP local + ffmpeg
                         RTMPS Facebook Live
```

## 1. Prérequis

- Backend Django déjà sur Railway (`azliveback`)
- Frontend sur Railway en HTTPS
- Même projet Railway (réseau privé activé)

## 2. Créer le service MediaMTX

1. Railway → ton projet → **New** → **Empty Service**
2. Connecte le même repo Git que le backend
3. **Settings** du service :
   - **Root Directory** : `mediamtx/railway`
   - **Builder** : Dockerfile
4. Génère un domaine public : **Settings → Networking → Generate Domain**  
   Exemple : `azlivemtx.up.railway.app`
5. (Recommandé) **Settings → Networking → Public Networking** : expose aussi le port **TCP 8189** (ICE) si Railway le propose

Nomme le service par ex. `azlivemtx` (le hostname privé sera `azlivemtx.railway.internal`).

## 3. Variables du service MediaMTX

| Variable | Exemple | Rôle |
|----------|---------|------|
| `MEDIAMTX_AUTH_URL` | `http://azliveback.railway.internal:$PORT/api/media/auth/` | Auth publish WHIP via Django — **remplace** `$PORT` par le port d’écoute du backend (souvent dans les vars Railway `PORT`, ou `8080` si tu forces `PORT=8080` côté backend) |
| `MEDIAMTX_PUBLIC_HOST` | `azlivemtx.up.railway.app` | Hostname public (sans `https://`) |
| `MEDIAMTX_API_PORT` | `9997` | API de contrôle (interne) |
| `MEDIAMTX_RTSP_PORT` | `8554` | RTSP interne ffmpeg |
| `MEDIAMTX_ICE_TCP_PORT` | `8189` | ICE TCP/UDP |

Astuce fiable pour l’auth : dans le service **backend**, fixe :

```env
PORT=8080
```

puis MediaMTX :

```env
MEDIAMTX_AUTH_URL=http://azliveback.railway.internal:8080/api/media/auth/
```

(adapte `azliveback` au **nom exact** du service backend dans Railway).

### Si WebRTC ne connecte pas (UDP bloqué)

Ajoute un TURN TCP (Metered, Twilio, Coturn…) :

```env
MEDIAMTX_TURN_URL=turn:xxx.metered.ca:80?transport=tcp
MEDIAMTX_TURN_USERNAME=...
MEDIAMTX_TURN_PASSWORD=...
```

## 4. Variables du backend Django (`azliveback`)

```env
MEDIAMTX_ENABLED=true
MEDIAMTX_API_URL=http://azlivemtx.railway.internal:9997
MEDIAMTX_WHIP_BASE_URL=https://azlivemtx.up.railway.app
MEDIAMTX_RTSP_HOST=127.0.0.1:8554
```

Important :

- `MEDIAMTX_API_URL` = hostname **privé** du service MediaMTX + port API
- `MEDIAMTX_WHIP_BASE_URL` = domaine **public HTTPS** (navigateur)
- `MEDIAMTX_RTSP_HOST=127.0.0.1:8554` car ffmpeg tourne **dans** le conteneur MediaMTX

Redeploy le backend après avoir sauvé les variables.

## 5. Test

1. Ouvre le front prod, connecte Facebook (vraie page, pas démo)
2. Crée un live → **Lancer**
3. Logs MediaMTX : publication WHIP + `ffmpeg` vers `rtmps://live-api-s.facebook.com...`
4. Logs backend : pas de `MediaMTX injoignable`
5. La page Facebook doit passer en direct avec ta caméra

## Dépannage

| Symptôme | Cause probable |
|----------|----------------|
| `MediaMTX injoignable` | Mauvais `MEDIAMTX_API_URL` / service pas sur le même projet |
| WHIP HTTP 401 | `MEDIAMTX_AUTH_URL` incorrect ou backend injoignable en private network |
| Live Graph OK mais pas de vidéo | ICE/UDP bloqué → expose TCP 8189 ou configure TURN |
| `whip_url` en `http://localhost` | Backend pas redéployé avec `MEDIAMTX_WHIP_BASE_URL` HTTPS |
