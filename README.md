# Plunkly
Tiny worlds. Big meetups. A space game for Telegram (WebApp) and the web.
- `web/` — game client (single page, GSAP, canvas particles)
- `app/` — FastAPI server: serves the game with cache busting, Telegram bot webhook
- `deploy/deploy.sh` — one-shot server deploy (systemd + nginx + Let's Encrypt), pushes a report to the `reports` branch
