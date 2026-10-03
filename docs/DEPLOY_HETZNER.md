# Deploying a demo on one server (Hetzner VPS)

This puts BookCRM on one Linux server with Docker: Caddy (HTTPS) in front, then the app,
the Celery worker and scheduler, PostgreSQL and Redis. It suits a demo or a small first
deployment. Everything is in `docker-compose.prod.yml`; `docs/DEPLOYMENT.md` lists the
settings it uses.

Only ports 22 (SSH), 80 and 443 are open. The database and Redis have no published ports.

## What you need

- A Hetzner Cloud server: **CX22** (2 vCPU, 4 GB) or bigger, **Ubuntu 24.04**, with your SSH
  key added when you create it. Create a Hetzner **firewall** allowing inbound TCP 22, 80,
  443 (and UDP 443) only, and attach it to the server.
- A domain or subdomain, e.g. `demo.yourcompany.com`, with an **A record** pointing at the
  server's IPv4 address (an AAAA record for IPv6 is optional). HTTPS needs it.

## 1. Prepare the server (once)

```sh
ssh root@SERVER_IP
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh           # Docker Engine + Compose plugin
adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
mkdir -p /home/deploy/.ssh && cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
```

Optional hardening: in `/etc/ssh/sshd_config` set `PasswordAuthentication no` and
`PermitRootLogin no`, then `systemctl restart ssh` (check you can log in as `deploy` first).
`apt install unattended-upgrades` keeps security updates coming.

## 2. Get the code

```sh
ssh deploy@SERVER_IP
git clone https://github.com/honkwok7/BookCRM.git bookcrm   # private repo: use a deploy key
cd bookcrm
```

## 3. Settings

```sh
cp .env.production.example .env.production
chmod 600 .env.production
nano .env.production
```

- Put your domain in `SITE_ADDRESS`, `DJANGO_ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`,
  `SITE_URL`, `APP_BASE_URL`.
- `DJANGO_SECRET_KEY`: `python3 -c "import secrets; print(secrets.token_hex(40))"`
- `POSTGRES_PASSWORD`: `openssl rand -hex 24`, and the same value inside `DATABASE_URL`.

Use letters and digits only in secrets (Docker Compose treats `$` specially).

To save typing, add an alias (put it in `~/.bashrc` to keep it):

```sh
alias dc='docker compose -f docker-compose.prod.yml --env-file .env.production'
```

## 4. Start

```sh
dc up -d --build
dc ps                 # web shows "healthy" after a minute
dc logs -f caddy      # watch it get the certificate, Ctrl+C to stop
```

The web container runs the migrations and collects static files each time it starts.

Open `https://your-domain/`. If the certificate fails, check that the A record points at the
server and that ports 80 and 443 are open.

## 5. Demo data (optional)

The demo accounts have published passwords, so give them your own on a public server:

```sh
dc exec web python manage.py seed_demo --allow-without-debug --password 'ChooseAStrongOne'
```

Every demo account (the platform admin `admin@bookcrm.local`, `owner@harmony.local`,
`alex@example.test`, ...) then has that password. Run it on a fresh database only.

For a real (non-demo) start instead, create your own platform admin:
`dc exec web python manage.py createsuperuser`.

## 6. Keep it private while reviewing (optional)

A site-wide username and password in front of everything: see `deploy/auth/README.md`.

## Everyday tasks

| Task | Command |
| --- | --- |
| Update to the latest code | `git pull && dc up -d --build` |
| Logs | `dc logs -f web` (or `celery`, `caddy`) |
| Emails (console backend) | `dc logs celery web \| grep -A20 Subject` |
| Django shell | `dc exec web python manage.py shell` |
| Restart | `dc restart` |
| Stop | `dc down` (data stays in the volumes) |

## Backups

The data lives in Docker volumes: `postgres_data` (the database) and `media` (uploads).

```sh
# Database dump (run daily from cron, copy it off the server)
dc exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > backup-$(date +%F).dump

# Restore into an empty database
dc exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < backup-2026-10-02.dump
```

Hetzner's server backups (20% of the server price) are a simple extra safety net.

## Trying it on your own computer first

With Docker Desktop you can run the same stack locally:

1. `cp .env.production.example .env.production`, replace `demo.example.com` with `localhost`
   everywhere and fill in the secrets.
2. Stop anything using ports 80 and 443 (or set `HTTP_PORT`/`HTTPS_PORT`).
3. `docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build`
4. Open `https://localhost/`. Caddy uses its own local certificate, so the browser warns once;
   continue anyway.
5. `docker compose -f docker-compose.prod.yml --env-file .env.production down -v` removes it
   all, data included.

## Before real customers use it

This setup is fine for a demo. For production, also:

- Send real email (an SMTP provider in the `EMAIL_*` settings).
- Turn on HSTS (`SECURE_HSTS_SECONDS`) once HTTPS works.
- Automate off-server backups and test a restore.
- Add uptime monitoring on `https://your-domain/health/`.
- Consider a managed PostgreSQL once there is real data.
