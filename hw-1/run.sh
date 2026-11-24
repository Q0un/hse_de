#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
umask 077

for tool in python3 docker; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Не найден %s. Установите Python 3.12+ и Docker с Compose.\n' "$tool" >&2
        exit 1
    fi
done
docker compose version >/dev/null
docker info >/dev/null

printf '\nПодготовка Python-окружения...\n'
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt

.venv/bin/python - <<'PY'
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

from etl import read_config

env_path = Path('.env')
if not env_path.exists():
    env_path.write_text(Path('.env.example').read_text(encoding='utf-8'), encoding='utf-8')
if dotenv_values(env_path).get('POSTGRES_PASSWORD') == 'replace_with_your_local_password':
    set_key(env_path, 'POSTGRES_PASSWORD', secrets.token_urlsafe(24))
env_path.chmod(0o600)
read_config(Path.cwd())
PY

printf '\nЗапуск PostgreSQL...\n'
docker compose up -d --wait --wait-timeout 120

printf '\nВыполнение ноутбука...\n'
.venv/bin/python -m ipykernel install --prefix .venv --name de-hw1 --display-name 'DE HW1 (.venv)'
.venv/bin/python -m nbconvert --to notebook --execute --inplace notebooks/etl.ipynb \
    --ExecutePreprocessor.kernel_name=de-hw1 --ExecutePreprocessor.timeout=900

docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT COUNT(*) FROM stg.population;"' </dev/null
printf '\nГотово. Результаты сохранены в notebooks/etl.ipynb, лог — в logs/etl.log.\n'
