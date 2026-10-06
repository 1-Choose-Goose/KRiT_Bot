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

PACKAGE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
echo "Установка серверной части КРиТ"
read -r -p "Домен сервера без https:// (пример: krit.example.ru): " KRIT_HOSTNAME
read -r -s -p "Токен MAX-бота: " MAX_BOT_TOKEN
echo
if [[ -z ${KRIT_HOSTNAME} || -z ${MAX_BOT_TOKEN} ]]; then
  echo "Домен и токен MAX обязательны."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx postgresql postgresql-client python3 python3-venv python3-pip \
  certbot python3-certbot-nginx ca-certificates openssl rsync sudo curl unzip

id -u krit >/dev/null 2>&1 || useradd --system --home /var/lib/krit --shell /usr/sbin/nologin krit
install -d -o krit -g krit -m 0700 /var/lib/krit /var/lib/krit/backups /var/lib/krit/restore
install -d -o root -g krit -m 0750 /etc/krit-bot
install -d -o root -g root -m 0755 /opt/krit-bot

rsync -a --delete "${PACKAGE_ROOT}/bot/" /opt/krit-bot/bot/
rsync -a --delete "${PACKAGE_ROOT}/deploy/" /opt/krit-bot/deploy/
python3 -m venv /opt/krit-bot/venv
/opt/krit-bot/venv/bin/python -m pip install --upgrade pip
/opt/krit-bot/venv/bin/python -m pip install -e /opt/krit-bot/bot
chown -R root:krit /opt/krit-bot/venv
chmod -R u=rwX,g=rX,o= /opt/krit-bot/venv

DB_PASSWORD=$(openssl rand -hex 24)
JWT_SECRET=$(openssl rand -hex 32)
WEBHOOK_SECRET=$(openssl rand -hex 32)

sudo -u postgres psql --set=ON_ERROR_STOP=1 --set=db_password="${DB_PASSWORD}" <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'krit') THEN
    CREATE ROLE krit LOGIN;
  END IF;
END
$$;
ALTER ROLE krit PASSWORD :'db_password';
SELECT 'CREATE DATABASE krit_bot OWNER krit'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'krit_bot')\gexec
SQL

cat >/etc/krit-bot/krit-bot.env <<EOF
DATABASE_URL=postgresql+asyncpg://krit:${DB_PASSWORD}@127.0.0.1:5432/krit_bot
KRIT_DATABASE_NAMES=krit_bot
KRIT_PGHOST=127.0.0.1
KRIT_PGPORT=5432
KRIT_PGUSER=krit
KRIT_PGPASSWORD=${DB_PASSWORD}
BACKUP_ROOT=/var/lib/krit/backups
RESTORE_ROOT=/var/lib/krit/restore
MAX_BOT_TOKEN=${MAX_BOT_TOKEN}
MAX_API_BASE_URL=https://platform-api2.max.ru
MAX_WEBHOOK_SECRET=${WEBHOOK_SECRET}
MAX_WEBHOOK_URL=https://${KRIT_HOSTNAME}/krit-api/webhooks/max
JWT_SECRET=${JWT_SECRET}
BOT_MODE=webhook
CENTER_TIMEZONE=Asia/Yekaterinburg
EOF
chown root:krit /etc/krit-bot/krit-bot.env
chmod 0640 /etc/krit-bot/krit-bot.env

install -o root -g root -m 0644 "${PACKAGE_ROOT}/deploy/krit-bot.service" /etc/systemd/system/krit-bot.service
install -o root -g root -m 0644 "${PACKAGE_ROOT}/deploy/krit-restore@.service" /etc/systemd/system/krit-restore@.service
install -o root -g root -m 0644 "${PACKAGE_ROOT}/deploy/krit-restore-rollback.service" /etc/systemd/system/krit-restore-rollback.service
install -o root -g root -m 0644 "${PACKAGE_ROOT}/deploy/krit-restore-delete.service" /etc/systemd/system/krit-restore-delete.service
install -o root -g root -m 0755 "${PACKAGE_ROOT}/deploy/krit_restore_dispatch.py" /usr/local/sbin/krit-restore-dispatch
install -o root -g root -m 0440 "${PACKAGE_ROOT}/deploy/krit-restore.sudoers" /etc/sudoers.d/krit-restore
visudo -cf /etc/sudoers.d/krit-restore

sed "s/__KRIT_HOSTNAME__/${KRIT_HOSTNAME}/g" "${PACKAGE_ROOT}/deploy/nginx-krit.conf" \
  >/etc/nginx/sites-available/krit
ln -sfn /etc/nginx/sites-available/krit /etc/nginx/sites-enabled/krit
nginx -t
systemctl reload nginx

set -a
. /etc/krit-bot/krit-bot.env
set +a
/opt/krit-bot/venv/bin/krit-migrate
systemctl daemon-reload
systemctl enable --now krit-bot.service

if ! certbot --nginx --non-interactive --agree-tos --register-unsafely-without-email \
  --redirect -d "${KRIT_HOSTNAME}"; then
  echo "КРиТ установлен, но HTTPS-сертификат не выпущен. Проверьте DNS домена и повторите certbot."
  exit 1
fi

systemctl restart krit-bot.service
curl --fail --silent --show-error "https://${KRIT_HOSTNAME}/krit-api/health" >/dev/null
touch /var/lib/krit/.installed
chown krit:krit /var/lib/krit/.installed
echo
echo "КРиТ установлен и запущен."
echo "Первый вход: Choose_Goose / 123"
echo "Программа сразу потребует задать новый пароль."
