#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "Запустите установщик от root: sudo bash deploy/install-krit-server.sh"
  exit 1
fi

if [[ ! -f /etc/os-release ]]; then
  echo "Не удалось определить операционную систему."
  exit 1
fi
. /etc/os-release
if [[ ${ID:-} != "ubuntu" && ${ID:-} != "debian" ]]; then
  echo "Поддерживаются Ubuntu и Debian."
  exit 1
fi

PYTHON_BIN=${PYTHON_BIN:-}
if [[ -z ${PYTHON_BIN} ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c \
    'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    PYTHON_BIN=$(command -v python3)
  elif command -v python3.13 >/dev/null 2>&1; then
    PYTHON_BIN=$(command -v python3.13)
  elif command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN=$(command -v python3.12)
  elif command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN=$(command -v python3.11)
  fi
fi
if [[ -z ${PYTHON_BIN} ]] || ! "${PYTHON_BIN}" -c \
  'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Для КРиТ требуется Python 3.11 или новее. Установка не изменяла службы сервера."
  exit 1
fi

PACKAGE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
EXISTING_ENV=/opt/krit-bot/config/krit-bot.env
if [[ -f ${EXISTING_ENV} ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${EXISTING_ENV}"
  set +a
  KRIT_HOSTNAME=${MAX_WEBHOOK_URL:-}
  KRIT_HOSTNAME=${KRIT_HOSTNAME#https://}
  KRIT_HOSTNAME=${KRIT_HOSTNAME%%/*}
  MAX_BOT_TOKEN=${MAX_BOT_TOKEN:-}
else
  KRIT_HOSTNAME=
  MAX_BOT_TOKEN=
fi
echo "Установка серверной части КРиТ"
if [[ -z ${KRIT_HOSTNAME} || -z ${MAX_BOT_TOKEN} ]]; then
  read -r -p "Домен сервера без https:// (пример: krit.example.ru): " KRIT_HOSTNAME
  read -r -s -p "Токен MAX-бота: " MAX_BOT_TOKEN
  echo
fi
if [[ -z ${KRIT_HOSTNAME} || -z ${MAX_BOT_TOKEN} ]]; then
  echo "Домен и токен MAX обязательны."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx postgresql postgresql-client python3 python3-venv python3-pip \
  certbot python3-certbot-nginx ca-certificates openssl rsync sudo curl unzip

id -u krit >/dev/null 2>&1 || useradd --system --home /opt/krit-bot/data --shell /usr/sbin/nologin krit
usermod --home /opt/krit-bot/data krit
install -d -o root -g root -m 0755 /opt/krit-bot
install -d -o root -g krit -m 0750 /opt/krit-bot/config
install -d -o krit -g krit -m 0700 \
  /opt/krit-bot/data /opt/krit-bot/data/restore /opt/krit-bot/data/online-assets

rsync -a --delete "${PACKAGE_ROOT}/bot/" /opt/krit-bot/bot/
rsync -a --delete "${PACKAGE_ROOT}/deploy/" /opt/krit-bot/deploy/
"${PYTHON_BIN}" -m venv /opt/krit-bot/venv
/opt/krit-bot/venv/bin/python -m pip install --no-cache-dir --upgrade pip 'setuptools>=83'
/opt/krit-bot/venv/bin/python -m pip install --no-cache-dir -e /opt/krit-bot/bot
chown -R root:krit /opt/krit-bot/venv
chmod -R u=rwX,g=rX,o= /opt/krit-bot/venv

DB_PASSWORD=${KRIT_PGPASSWORD:-}
DB_PASSWORD=${DB_PASSWORD:-$(openssl rand -hex 24)}
DB_ROLE=${KRIT_PGUSER:-krit}
if [[ ! ${DB_ROLE} =~ ^[a-zA-Z][a-zA-Z0-9_]{0,62}$ ]]; then
  echo "Некорректное имя роли PostgreSQL."
  exit 1
fi
JWT_SECRET=${JWT_SECRET:-$(openssl rand -hex 32)}
WEBHOOK_SECRET=${MAX_WEBHOOK_SECRET:-$(openssl rand -hex 32)}
ONLINE_OTP_PEPPER=${ONLINE_OTP_PEPPER:-$(openssl rand -hex 32)}

sudo -u postgres psql --set=ON_ERROR_STOP=1 \
  --set=db_role="${DB_ROLE}" --set=db_password="${DB_PASSWORD}" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'db_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'db_role')\gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'db_role', :'db_password')\gexec
SELECT format('ALTER ROLE %I CREATEDB', :'db_role')\gexec
SELECT format('CREATE DATABASE krit_bot OWNER %I', :'db_role')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'krit_bot')\gexec
SQL

if [[ ! -f ${EXISTING_ENV} ]]; then
  cat >"${EXISTING_ENV}" <<EOF
DATABASE_URL=postgresql+asyncpg://krit:${DB_PASSWORD}@127.0.0.1:5432/krit_bot
KRIT_DATABASE_NAMES=krit_bot
KRIT_PGHOST=127.0.0.1
KRIT_PGPORT=5432
KRIT_PGUSER=krit
KRIT_PGPASSWORD=${DB_PASSWORD}
RESTORE_ROOT=/opt/krit-bot/data/restore
ONLINE_UPLOAD_ROOT=/opt/krit-bot/data/online-assets
ONLINE_UPLOAD_LIMIT_BYTES=15728640
ONLINE_ROOM_STORAGE_LIMIT_BYTES=104857600
ONLINE_TOTAL_STORAGE_LIMIT_BYTES=314572800
ONLINE_MIN_FREE_DISK_BYTES=1073741824
ONLINE_CLEANUP_INTERVAL_SECONDS=900
ONLINE_ABANDONED_ROOM_HOURS=24
ONLINE_PUBLIC_URL=https://${KRIT_HOSTNAME}/online.html
ONLINE_ALLOWED_ORIGINS=https://${KRIT_HOSTNAME}
ONLINE_COOKIE_SECURE=true
ONLINE_SESSION_HOURS=12
ONLINE_OTP_PEPPER=${ONLINE_OTP_PEPPER}
MAX_BOT_TOKEN=${MAX_BOT_TOKEN}
MAX_API_BASE_URL=https://platform-api2.max.ru
MAX_WEBHOOK_SECRET=${WEBHOOK_SECRET}
MAX_WEBHOOK_URL=https://${KRIT_HOSTNAME}/krit-api/webhooks/max
JWT_SECRET=${JWT_SECRET}
BOT_MODE=webhook
CENTER_TIMEZONE=Asia/Yekaterinburg
EOF
fi
grep -q '^ONLINE_UPLOAD_ROOT=' "${EXISTING_ENV}" || cat >>"${EXISTING_ENV}" <<EOF
ONLINE_UPLOAD_ROOT=/opt/krit-bot/data/online-assets
ONLINE_UPLOAD_LIMIT_BYTES=15728640
ONLINE_ROOM_STORAGE_LIMIT_BYTES=104857600
ONLINE_TOTAL_STORAGE_LIMIT_BYTES=314572800
ONLINE_MIN_FREE_DISK_BYTES=1073741824
ONLINE_CLEANUP_INTERVAL_SECONDS=900
ONLINE_ABANDONED_ROOM_HOURS=24
ONLINE_PUBLIC_URL=https://${KRIT_HOSTNAME}/online.html
ONLINE_ALLOWED_ORIGINS=https://${KRIT_HOSTNAME}
ONLINE_COOKIE_SECURE=true
ONLINE_SESSION_HOURS=12
ONLINE_OTP_PEPPER=${ONLINE_OTP_PEPPER}
EOF
chown root:krit /opt/krit-bot/config/krit-bot.env
chmod 0640 /opt/krit-bot/config/krit-bot.env

install -o root -g root -m 0644 "${PACKAGE_ROOT}/deploy/krit-bot.service" /etc/systemd/system/krit-bot.service
install -o root -g root -m 0644 "${PACKAGE_ROOT}/deploy/krit-restore@.service" /etc/systemd/system/krit-restore@.service
install -o root -g root -m 0644 "${PACKAGE_ROOT}/deploy/krit-restore-rollback.service" /etc/systemd/system/krit-restore-rollback.service
install -o root -g root -m 0644 "${PACKAGE_ROOT}/deploy/krit-restore-delete.service" /etc/systemd/system/krit-restore-delete.service
chmod 0755 /opt/krit-bot/deploy/krit_restore_dispatch.py /opt/krit-bot/deploy/krit_nginx_configure.py
install -o root -g root -m 0440 "${PACKAGE_ROOT}/deploy/krit-restore.sudoers" /etc/sudoers.d/krit-restore
visudo -cf /etc/sudoers.d/krit-restore

install -o root -g root -m 0644 "${PACKAGE_ROOT}/deploy/nginx-krit-api.conf" /etc/nginx/snippets/krit-api.conf
sed "s/__KRIT_HOSTNAME__/${KRIT_HOSTNAME}/g" "${PACKAGE_ROOT}/deploy/nginx-krit.conf" \
  >/etc/nginx/sites-available/krit
ln -sfn /etc/nginx/sites-available/krit /etc/nginx/sites-enabled/krit
if /opt/krit-bot/deploy/krit_nginx_configure.py "${KRIT_HOSTNAME}"; then
  echo "KRiT API подключён к существующему HTTPS-сайту."
fi
nginx -t
systemctl reload nginx

set -a
. /opt/krit-bot/config/krit-bot.env
set +a
/opt/krit-bot/venv/bin/krit-migrate
systemctl daemon-reload
systemctl enable --now krit-bot.service

if ! certbot --nginx --non-interactive --agree-tos --register-unsafely-without-email \
  --redirect -d "${KRIT_HOSTNAME}"; then
  echo "КРиТ установлен, но HTTPS-сертификат не выпущен. Проверьте DNS домена и повторите certbot."
  exit 1
fi

/opt/krit-bot/deploy/krit_nginx_configure.py "${KRIT_HOSTNAME}"
nginx -t
systemctl reload nginx
systemctl restart krit-bot.service
ready=0
for attempt in {1..30}; do
  if curl --fail --silent --show-error "https://${KRIT_HOSTNAME}/krit-api/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ ${ready} -ne 1 ]]; then
  echo "KRiT не прошёл HTTPS-проверку готовности за 60 секунд."
  exit 1
fi
touch /opt/krit-bot/data/.installed
chown krit:krit /opt/krit-bot/data/.installed
echo
echo "КРиТ установлен и запущен."
echo "Первый вход: Choose_Goose / 123"
echo "Программа сразу потребует задать новый пароль."
