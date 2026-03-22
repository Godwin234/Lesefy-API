# Lesefy API — Production Server Setup Spec

Functional specification for an agent provisioning a fresh Linux server to run the
Lesefy API backend in production.  The application code arrives via `git pull`; this
document covers everything else.

---

## Table of Contents
1. [Target Stack Summary](#1-target-stack-summary)
2. [OS & System Packages](#2-os--system-packages)
3. [Python 3.11 Install](#3-python-311-install)
4. [MongoDB 7 Install & Config](#4-mongodb-7-install--config)
5. [Redis Install & Config](#5-redis-install--config)
6. [Application Setup](#6-application-setup)
7. [Environment Variables (.env)](#7-environment-variables-env)
8. [Firebase Credentials](#8-firebase-credentials)
9. [Upload Directories](#9-upload-directories)
10. [Python Dependencies](#10-python-dependencies)
11. [Database Indexes](#11-database-indexes)
12. [Production WSGI Server (Gunicorn + Eventlet)](#12-production-wsgi-server-gunicorn--eventlet)
13. [Systemd Service Units](#13-systemd-service-units)
14. [Nginx Reverse Proxy](#14-nginx-reverse-proxy)
15. [Stripe Webhook Registration](#15-stripe-webhook-registration)
16. [Verification Checklist](#16-verification-checklist)

---

## 1. Target Stack Summary

| Component | Version | Role |
|-----------|---------|------|
| Ubuntu | 20.04 LTS or 22.04 LTS | OS |
| Python | **3.11.x** | Runtime |
| Flask | 3.1 | Web framework |
| Gunicorn + eventlet | latest | Production WSGI / WebSocket server |
| MongoDB | **7.0** | Primary database |
| Redis | **5.x or 7.x** | Cache + session store |
| Nginx | latest stable | TLS termination + reverse proxy |

---

## 2. OS & System Packages

```bash
sudo apt-get update && sudo apt-get upgrade -y

# Core build tools
sudo apt-get install -y \
  build-essential git curl wget gnupg lsb-release ca-certificates \
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev \
  libgl1-mesa-glx libglib2.0-0 \
  nginx supervisor ufw

# EasyOCR / OpenCV system libs
sudo apt-get install -y \
  libgomp1 libglu1-mesa libsm6 libxext6 libxrender-dev \
  tesseract-ocr

# PyMuPDF (PDF rendering)
sudo apt-get install -y libmupdf-dev
```

> **GPU note**: EasyOCR will automatically use CPU if no CUDA GPU is present.
> If you have a CUDA GPU, install the CUDA toolkit matching the `torch==2.10.0`
> version (CUDA 12.x) before installing Python packages.  CPU-only works fine
> for production at moderate receipt volume.

---

## 3. Python 3.11 Install

Use **pyenv** (recommended) to pin Python 3.11.7 without touching system Python.

```bash
# Install pyenv
curl https://pyenv.run | bash

# Add to shell profile (~/.bashrc or ~/.profile)
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
echo 'export PATH="$PYENV_ROOT/bin:$PATH"'  >> ~/.bashrc
echo 'eval "$(pyenv init -)"'  >> ~/.bashrc
source ~/.bashrc

# Install Python 3.11.7
pyenv install 3.11.7
pyenv global 3.11.7
python --version   # should print Python 3.11.7

# Update pip and install pipenv
pip install --upgrade pip
pip install pipenv
```

---

## 4. MongoDB 7 Install & Config

### Install

```bash
# Import MongoDB 7.0 GPG key and apt source
curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc \
  | sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor

echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] \
  https://repo.mongodb.org/apt/ubuntu $(lsb_release -cs)/mongodb-org/7.0 multiverse" \
  | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list

sudo apt-get update
sudo apt-get install -y mongodb-org

sudo systemctl enable --now mongod
sudo systemctl status mongod   # verify Active: running
```

### Secure MongoDB (production)

```bash
# Open the mongo shell
mongosh

# Inside mongosh — create an admin user first
use admin
db.createUser({
  user: "lesefyAdmin",
  pwd:  "<STRONG_ADMIN_PASSWORD>",
  roles: [{ role: "userAdminAnyDatabase", db: "admin" }, "readWriteAnyDatabase"]
})

# Create the app database and its dedicated user
use lesefy
db.createUser({
  user: "lesefyApp",
  pwd:  "<STRONG_APP_PASSWORD>",
  roles: [{ role: "readWrite", db: "lesefy" }]
})
exit
```

Enable authentication in `/etc/mongod.conf`:

```yaml
security:
  authorization: enabled
```

```bash
sudo systemctl restart mongod
```

The MONGO_URI in `.env` becomes:
```
MONGO_URI=mongodb://lesefyApp:<STRONG_APP_PASSWORD>@127.0.0.1:27017/lesefy?authSource=lesefy
```

### Collections created automatically by the app

The application creates all collections implicitly on first write.  The following
collections will be present after the first run:

| Collection | Module |
|------------|--------|
| `user` | auth / properties |
| `password` | auth |
| `property` | properties |
| `maintenance` | maintenance |
| `activity` | activities |
| `conversation`, `message` | chat |
| `notification`, `push_token` | notifications |
| `document` | documents |
| `receipt` | receipts |
| `transaction` | transactions |
| `background_check` | background_checks |
| `stripe_data` | stripe_finance |
| `rent_payment` | rent |

---

## 5. Redis Install & Config

```bash
sudo apt-get install -y redis-server

# Enable persistence + set a password (recommended in production)
sudo sed -i 's/^# requirepass .*/requirepass <REDIS_PASSWORD>/' /etc/redis/redis.conf
sudo sed -i 's/^supervised no/supervised systemd/' /etc/redis/redis.conf

sudo systemctl enable --now redis-server
redis-cli -a <REDIS_PASSWORD> ping   # should return PONG
```

Update REDIS_URL in `.env`:
```
REDIS_URL=redis://:< REDIS_PASSWORD>@127.0.0.1:6379/0
```

---

## 6. Application Setup

```bash
# Create a dedicated system user (optional but recommended)
sudo useradd -m -s /bin/bash lesefy

# Clone the repository
sudo -u lesefy -i
cd /home/lesefy
git clone git@github.com:Godwin234/Lesefy-API.git
cd Lesefy-API
```

> The deployment user needs read access to the git repo.  Set up an SSH deploy
> key on the server and add it to the GitHub repository's Deploy Keys.

---

## 7. Environment Variables (.env)

Create `/home/lesefy/Lesefy-API/.env` with the following variables.
**Never commit this file.**

```dotenv
# ── JWT ─────────────────────────────────────────────────────────────────────
# Must be >= 32 bytes.  Generate with: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=<64-char-hex-string>

# ── MongoDB ──────────────────────────────────────────────────────────────────
MONGO_URI=mongodb://lesefyApp:<STRONG_APP_PASSWORD>@127.0.0.1:27017/lesefy?authSource=lesefy
MONGO_DB_NAME=lesefy

# ── Redis ────────────────────────────────────────────────────────────────────
REDIS_URL=redis://:<REDIS_PASSWORD>@127.0.0.1:6379/0
CACHE_TYPE=RedisCache
CACHE_DEFAULT_TIMEOUT=300

# ── Stripe ───────────────────────────────────────────────────────────────────
# Get these from the Stripe Dashboard → Developers
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...

# ── Firebase (FCM push notifications) ───────────────────────────────────────
# Absolute path to the service account JSON file on this server
FIREBASE_CREDENTIALS_PATH=/home/lesefy/Lesefy-API/secrets/firebase-service-account.json
```

> `FIREBASE_CREDENTIALS_PATH` is optional — if omitted, push notifications via FCM
> are silently disabled.  Expo push tokens still work without it.

---

## 8. Firebase Credentials

1. In the Firebase Console → Project Settings → Service Accounts → **Generate new private key**.
2. Download the JSON file.
3. Upload it to the server:
   ```bash
   mkdir -p /home/lesefy/Lesefy-API/secrets
   scp firebase-service-account.json lesefy@<SERVER>:/home/lesefy/Lesefy-API/secrets/
   chmod 600 /home/lesefy/Lesefy-API/secrets/firebase-service-account.json
   ```
4. Set `FIREBASE_CREDENTIALS_PATH` in `.env` to that absolute path.

---

## 9. Upload Directories

The application stores user-uploaded files under `uploads/` relative to the repo root.
Create all subdirectories and set correct permissions:

```bash
cd /home/lesefy/Lesefy-API
mkdir -p uploads/Documents \
         uploads/ListingPictures \
         uploads/MaintenancePictures \
         uploads/MoveInPictures \
         uploads/MoveOutPictures \
         uploads/ProfilePicture \
         uploads/Receipts \
         uploads/RecieptPictures \
         uploads/SignedDocuments \
         uploads/TransactionImages

chmod -R 755 uploads/
```

Nginx or the OS must not strip write access from the `lesefy` user on these directories.

---

## 10. Python Dependencies

All packages are pinned in `Pipfile`.  Install them into an isolated virtualenv:

```bash
cd /home/lesefy/Lesefy-API

# Install all packages (will resolve from Pipfile)
pipenv install --deploy

# Add gunicorn + eventlet (production WSGI — not in Pipfile yet)
pipenv install gunicorn eventlet
```

### Key heavy packages and what they need

| Package | Notes |
|---------|-------|
| `easyocr==1.7.2` | Downloads ~300 MB model files on **first use**. Pre-warm by running `pipenv run python -c "import easyocr; easyocr.Reader(['en'])"` once after install. |
| `torch==2.10.0` | Large (~2 GB). CPU-only install is fine. If CUDA is present, torch will use it automatically. |
| `PyMuPDF==1.27.2` | Requires `libmupdf` system lib (installed in step 2). |
| `firebase_admin==7.2.0` | Requires the service account JSON at startup if FCM is enabled. |
| `stripe==14.4.1` | Needs `STRIPE_SECRET_KEY` in env. |
| `opencv-python-headless` | Headless build — no display needed (correct for server). |

---

## 11. Database Indexes

**All indexes are created automatically** when the Flask app starts for the first time.
The `create_app()` factory calls each module's `ensure_*_indexes(db)` function inside
an `app_context`.  No manual index creation is required.

Indexes created on startup:

```
conversation   : participants, participantEmails, updatedAt
message        : (conversationId+createdAt), (conversationId+readBy)
notification   : (userId+createdAt), (userId+read)
push_token     : (userId+deviceId) unique sparse, token unique
document       : (ownerId+status), (signers.userId+status), propertyId, updatedAt
receipt        : userId, createdAt, transactionType, propertyId sparse
transaction    : userId, type, receiptId sparse, rentId sparse, propertyId sparse, createdAt
background_check: landlordId, tenantId, status, propertyId sparse, createdAt
stripe_data    : user_id unique
rent_payment   : tenantId, landlordId, propertyId, (tenantId+period),
                 status, stripeChargeId sparse+unique
```

---

## 12. Production WSGI Server (Gunicorn + Eventlet)

The app uses **Flask-SocketIO** for WebSocket support.  Gunicorn must use the
**eventlet** worker so WebSocket upgrade requests are handled correctly.

Create `/home/lesefy/Lesefy-API/gunicorn.conf.py`:

```python
import multiprocessing

# Eventlet worker is required for Flask-SocketIO
worker_class = "eventlet"

# 1 worker for SocketIO (multiple workers break in-memory rooms).
# Scale with a message queue (Redis pub/sub) if you need > 1 worker later.
workers = 1

# Tune to available CPU cores * 1000 for async workloads
worker_connections = 1000

# Bind
bind = "127.0.0.1:5000"

# Timeouts — raise for long OCR requests
timeout  = 120
keepalive = 5

# Logging
accesslog = "/var/log/lesefy/access.log"
errorlog  = "/var/log/lesefy/error.log"
loglevel  = "info"

# Preload the app (faster reload, but eventlet works better without it for SocketIO)
preload_app = False
```

Create the log directory:
```bash
sudo mkdir -p /var/log/lesefy
sudo chown lesefy:lesefy /var/log/lesefy
```

Test manually before wiring into systemd:
```bash
cd /home/lesefy/Lesefy-API
pipenv run gunicorn -c gunicorn.conf.py "main:app"
# Should print: Listening at: http://127.0.0.1:5000
```

---

## 13. Systemd Service Units

### API service

Create `/etc/systemd/system/lesefy-api.service`:

```ini
[Unit]
Description=Lesefy API (Gunicorn + Eventlet)
After=network.target mongod.service redis-server.service

[Service]
User=lesefy
Group=lesefy
WorkingDirectory=/home/lesefy/Lesefy-API
EnvironmentFile=/home/lesefy/Lesefy-API/.env
ExecStart=/home/lesefy/.local/share/virtualenvs/Lesefy-API-<HASH>/bin/gunicorn \
          -c gunicorn.conf.py "main:app"
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/lesefy/systemd.log
StandardError=append:/var/log/lesefy/systemd.log

[Install]
WantedBy=multi-user.target
```

> Replace `<HASH>` with the actual virtualenv hash.  Find it with:
> `pipenv --venv` — it will print something like
> `/home/lesefy/.local/share/virtualenvs/Lesefy-API-aRW_1xEx`

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lesefy-api
sudo systemctl status lesefy-api
```

---

## 14. Nginx Reverse Proxy

Create `/etc/nginx/sites-available/lesefy`:

```nginx
upstream lesefy_backend {
    server 127.0.0.1:5000;
}

server {
    listen 80;
    server_name api.yourdomain.com;

    # Redirect HTTP → HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;

    # TLS — obtain cert with: certbot --nginx -d api.yourdomain.com
    ssl_certificate     /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 15M;   # must >= MAX_CONTENT_LENGTH in app (10 MB) + headroom

    # WebSocket upgrade (Flask-SocketIO)
    location /socket.io/ {
        proxy_pass         http://lesefy_backend;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "Upgrade";
        proxy_set_header   Host       $host;
        proxy_set_header   X-Real-IP  $remote_addr;
        proxy_read_timeout 86400;   # keep WS connections alive
    }

    # REST API + file uploads
    location / {
        proxy_pass         http://lesefy_backend;
        proxy_set_header   Host             $host;
        proxy_set_header   X-Real-IP        $remote_addr;
        proxy_set_header   X-Forwarded-For  $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_read_timeout 120s;            # allow time for OCR requests
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/lesefy /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Obtain a free TLS certificate
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.yourdomain.com
```

---

## 15. Stripe Webhook Registration

After the server is live and reachable via HTTPS:

1. In the **Stripe Dashboard** → Developers → Webhooks → **Add endpoint**.
2. Endpoint URL: `https://api.yourdomain.com/api/stripe/webhook`
3. Select events to listen for:
   - `charge.succeeded`
   - `charge.failed`
   - `customer.deleted`
   - `payment_method.attached`
   - `payment_method.detached`
4. Click **Add endpoint**, then reveal and copy the **Signing secret** (`whsec_...`).
5. Set `STRIPE_WEBHOOK_SECRET=whsec_...` in `.env`.
6. Restart the service: `sudo systemctl restart lesefy-api`

---

## 16. Verification Checklist

Run these commands after the setup is complete:

```bash
# 1. MongoDB reachable
mongosh "mongodb://lesefyApp:<pw>@127.0.0.1:27017/lesefy?authSource=lesefy" --eval "db.runCommand({ping:1})"

# 2. Redis reachable
redis-cli -a <REDIS_PASSWORD> ping

# 3. App starts without errors
cd /home/lesefy/Lesefy-API
pipenv run python -c "from app import create_app; app = create_app(); print('OK')"

# 4. Gunicorn starts and listens
sudo systemctl status lesefy-api

# 5. Health endpoint responds
curl -s http://127.0.0.1:5000/api/health | python3 -m json.tool

# 6. Nginx proxies correctly
curl -sk https://api.yourdomain.com/api/health

# 7. WebSocket handshake (basic smoke test)
curl -sk "https://api.yourdomain.com/socket.io/?EIO=4&transport=polling" | head -c 100

# 8. Indexes exist in MongoDB
mongosh lesefy --eval "db.rent_payment.getIndexes().forEach(i => print(i.name))"
```

### Firewall (ufw)

```bash
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP (redirects to HTTPS)
sudo ufw allow 443/tcp   # HTTPS
sudo ufw enable
# MongoDB (27017) and Redis (6379) should NOT be open externally
```

---

## Quick-Reference: All Environment Variables

| Variable | Required | Example |
|----------|----------|---------|
| `SECRET_KEY` | **Yes** | 64-char hex string |
| `MONGO_URI` | **Yes** | `mongodb://user:pass@127.0.0.1:27017/lesefy?authSource=lesefy` |
| `MONGO_DB_NAME` | No (default: `lesefy`) | `lesefy` |
| `REDIS_URL` | No (default: `redis://localhost:6379/0`) | `redis://:pass@127.0.0.1:6379/0` |
| `CACHE_TYPE` | No (default: `RedisCache`) | `RedisCache` |
| `CACHE_DEFAULT_TIMEOUT` | No (default: `300`) | `300` |
| `STRIPE_SECRET_KEY` | **Yes** (for payments) | `sk_live_...` |
| `STRIPE_WEBHOOK_SECRET` | **Yes** (for webhooks) | `whsec_...` |
| `FIREBASE_CREDENTIALS_PATH` | No (FCM disabled if absent) | `/home/lesefy/Lesefy-API/secrets/firebase-service-account.json` |
