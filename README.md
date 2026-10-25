# Rideshare match server

Flask-SocketIO match server prepared for Google Cloud Run.

## Deploy

From this directory:

```sh
set -a
. ./.env.local
set +a

gcloud run deploy rideshare-match \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --timeout 3600 \
  --concurrency 80 \
  --max-instances 1 \
  --set-env-vars "CORS_ALLOWED_ORIGINS=https://rideware.vercel.app,NEXT_PUBLIC_STREAM_API_KEY=${NEXT_PUBLIC_STREAM_API_KEY},STREAM_API_SECRET=${STREAM_API_SECRET}"
```

`.env.local` supplies `NEXT_PUBLIC_STREAM_API_KEY` and `STREAM_API_SECRET`. `CORS_ALLOWED_ORIGINS` must be the exact browser origin, with no trailing slash.

The web app chooses its match server in `rideshare-web/app/public-config.ts`. Set `useLocalMatchServer` to `false` for Cloud Run and `true` for localhost. The web app does not use Vercel environment variables.

## Why one instance?

The waiting lobby is held in this process's memory. `--max-instances 1` ensures both
riders reach the same lobby. Before allowing Cloud Run to scale beyond one instance,
move lobby state and Socket.IO coordination to shared infrastructure such as Redis.

Cloud Run WebSocket requests are still subject to the configured request timeout.
The deployment command uses Cloud Run's 60-minute maximum; the web client should
reconnect and rejoin after a timeout if longer waits are required.

## Configuration

- `PORT`: Set automatically by Cloud Run. Defaults to `5050` outside the container.
- `HOST`: Defaults to `0.0.0.0`.
- `CORS_ALLOWED_ORIGINS`: Comma-separated additional HTTPS web origins.
- `NEXT_PUBLIC_STREAM_API_KEY`: Public Stream key. Set from `.env.local` on Cloud Run.
- `STREAM_API_SECRET`: Stream server secret. Required for chat tokens and channels.
  Set from `.env.local` on Cloud Run. Locally it is read from `.env.local` or `.env`.
  Both files are gitignored and excluded from the container image.
- `SECRET_KEY`: Optional Flask secret. Set this through Secret Manager for production.

Localhost origins on ports 3000 and 3001 remain enabled for development.

## Local container test

```sh
docker build -t rideshare-match .
docker run --rm -p 8080:8080 rideshare-match
curl http://localhost:8080/health
```
