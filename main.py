#!/usr/bin/env python3
"""Stepik course backup.

Saves each step as:
  <dir>/<lesson_id>_<pos>_<block_name>.md    — читаемый Markdown
  <dir>/<lesson_id>_<pos>_<block_name>.json  — полный дамп step-source

Установка:   pip install -r requirements.txt
Запуск:      python3 main.py
"""
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
    from dotenv import load_dotenv
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as exc:
    sys.exit(
        f'Missing dependency: {exc.name}.\n'
        f'Install with: pip install -r requirements.txt'
    )

# --- .env ------------------------------------------------------------------
load_dotenv(Path(__file__).with_name('.env'))


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f'Environment variable {name} is not set. Check .env')
    return value


# --- Config ----------------------------------------------------------------
client_id = require_env('STEPIK_CLIENT_ID')
client_secret = require_env('STEPIK_CLIENT_SECRET')
username = require_env('STEPIK_USERNAME')
password = require_env('STEPIK_PASSWORD')

api_host = os.environ.get('STEPIK_API_HOST', 'https://stepik.org').rstrip('/')
course_id = int(require_env('STEPIK_COURSE_ID'))
output_dir = Path(f'stepik_course_{course_id}')
skip_existing = os.environ.get('STEPIK_SKIP_EXISTING', 'true').lower() in {'1', 'true', 'yes', 'on'}
max_workers = int(os.environ.get('STEPIK_MAX_WORKERS', '8'))
timeout = (10, 60)  # (connect, read), seconds
batch_size = int(os.environ.get('STEPIK_BATCH_SIZE', '5'))

if batch_size < 1:
    raise RuntimeError('STEPIK_BATCH_SIZE must be a positive integer')


def log(message):
    print(message, flush=True)


# --- HTTP session with retries ---------------------------------------------
session = requests.Session()
session.headers['User-Agent'] = 'stepik-backup/1.1'

retries = Retry(
    total=5,
    backoff_factor=1,                # 1s, 2s, 4s, 8s, 16s
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({'GET', 'POST'}),
    respect_retry_after_header=True,
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=retries)
session.mount('https://', _adapter)
session.mount('http://', _adapter)

_auth_lock = threading.Lock()


# --- Auth ------------------------------------------------------------------
def _do_authenticate():
    response = session.post(
        f'{api_host}/oauth2/token/',
        data={
            'grant_type': 'password',
            'client_id': client_id,
            'client_secret': client_secret,   # обязателен для confidential
            'username': username,
            'password': password,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    token = response.json().get('access_token')
    if not token:
        raise RuntimeError('Stepik did not return an access token')
    session.headers['Authorization'] = f'Bearer {token}'


def authenticate():
    with _auth_lock:
        _do_authenticate()


def _refresh_token_if_stale(failed_token):
    """Re-authenticate only if another thread hasn't refreshed the token yet."""
    with _auth_lock:
        if session.headers.get('Authorization') == failed_token:
            _do_authenticate()


# --- API helpers -----------------------------------------------------------
def get_api_response(api_url, params=None):
    response = session.get(api_url, params=params, timeout=timeout)

    if response.status_code == 401:
        failed = session.headers.get('Authorization')
        log('Token expired, re-authenticating...')
        _refresh_token_if_stale(failed)
        response = session.get(api_url, params=params, timeout=timeout)

    if response.status_code == 403:
        raise PermissionError(
            'Stepik denied access to protected course content. Check that the '
            'OAuth application and its account have access to this course.'
        )
    response.raise_for_status()
    return response


def fetch_object(obj_class, obj_id):
    api_url = '{}/api/{}s/{}'.format(api_host, obj_class, obj_id)
    return get_api_response(api_url).json()['{}s'.format(obj_class)][0]


def fetch_objects(obj_class, obj_ids):
    objs = []
    # Stepik can return 504 even for sections and units when the request has
    # a large ids[] list.  Keep every bulk endpoint in small, retryable chunks.
    for i in range(0, len(obj_ids), batch_size):
        chunk = obj_ids[i:i + batch_size]
        log(
            f'Метаданные {obj_class}: {i + 1}-'
            f'{min(i + len(chunk), len(obj_ids))} из {len(obj_ids)}'
        )
        api_url = '{}/api/{}s'.format(api_host, obj_class)
        response = get_api_response(api_url, params=[('ids[]', x) for x in chunk])
        objs += response.json()['{}s'.format(obj_class)]
    return objs


# --- Formatting ------------------------------------------------------------
def get_valid_filename(s):
    name = re.sub(r'(?u)[^-\w. ]', '', str(s)).strip()
    return name or 'untitled'


def step_to_markdown(course, section, lesson, step, step_source):
    block = step_source['block']
    source = block.get('source', {})
    parts = [
        f'# {lesson["title"]}',
        f'> Курс: {course["title"]}  ',
        f'> Раздел: {section["title"]}  ',
        f'> Шаг Stepik: {step["id"]} ({block["name"]})',
    ]

    text = block.get('text')
    if text:
        parts.extend(['', text.strip()])

    if block['name'] == 'choice':
        options = source.get('options', [])
        if options:
            multiple = 'несколько вариантов' if source.get('is_multiple_choice') else 'один вариант'
            parts.extend(['', f'## Варианты ответа ({multiple})', ''])
            for option in options:
                mark = 'x' if option.get('is_correct') else ' '
                parts.append(f'- [{mark}] {option.get("text", "")}')
                if option.get('feedback'):
                    parts.append(f'  - Обратная связь: {option["feedback"]}')

    if block['name'] == 'number':
        answers = source.get('options', [])
        if answers:
            parts.extend(['', '## Ответы', ''])
            for answer in answers:
                parts.append(
                    f'- `{answer.get("answer", "")}` '
                    f'(допуск: `{answer.get("max_error", "0")}`)'
                )

    return '\n'.join(parts)


# --- Step processing -------------------------------------------------------
def process_step(course, section, lesson, step, md_path, json_path):
    """Fetch step-source and write .md + .json. Returns (status, md_path)."""
    if skip_existing and md_path.exists() and json_path.exists():
        return ('skip', md_path)

    step_source = fetch_object('step-source', step['id'])

    md_path.parent.mkdir(parents=True, exist_ok=True)

    md_path.write_text(
        step_to_markdown(course, section, lesson, step, step_source),
        encoding='utf-8',
    )
    json_path.write_text(
        json.dumps(step_source, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return ('saved', md_path)


# --- Main ------------------------------------------------------------------
def main():
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f'Не удалось создать папку для резервной копии: {exc}') from exc
    log(f'Папка для сохранения: {output_dir.resolve()}')

    authenticate()
    log('Авторизация выполнена.')

    course = fetch_object('course', course_id)
    log(f'Курс: {course["id"]} — {course["title"]}')

    log(f'Загружаю метаданные курса: разделов {len(course["sections"])}.')
    sections = fetch_objects('section', course['sections'])

    # Собираем все задачи последовательно (это быстро — только метаданные).
    tasks = []
    for section in sections:
        log(f'Раздел: {section["title"]}')
        unit_ids = section.get('units') or []
        if not unit_ids:
            log(f'  Раздел {section["id"]} пуст, пропускаем.')
            continue
        units = fetch_objects('unit', unit_ids)
        for unit in units:
            lesson = fetch_object('lesson', unit['lesson'])
            log(f'  Урок: {lesson["title"]}')
            step_ids = lesson.get('steps') or []
            if not step_ids:
                log(f'  Урок {lesson["id"]} без шагов, пропускаем.')
                continue
            steps = fetch_objects('step', step_ids)

            dir_path = output_dir / Path(
                '{} {}'.format(
                    str(course['id']).zfill(2),
                    get_valid_filename(course['title']),
                ),
                '{} {}'.format(
                    str(section.get('position', 0)).zfill(2),
                    get_valid_filename(section['title']),
                ),
                '{} {}'.format(
                    str(unit.get('position', 0)).zfill(2),
                    get_valid_filename(lesson['title']),
                ),
            )
            for step in steps:
                base = '{}_{}_{}'.format(
                    lesson['id'],
                    str(step.get('position', 0)).zfill(2),
                    step['block']['name'],
                )
                tasks.append((
                    course, section, lesson, step,
                    dir_path / (base + '.md'),
                    dir_path / (base + '.json'),
                ))

    log(f'Метаданные получены. Всего шагов: {len(tasks)}.')
    log(f'Начинаю сохранение файлов. Параллельных воркеров: {max_workers}.')

    errors = []
    saved = 0
    skipped = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {executor.submit(process_step, *t): t for t in tasks}
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            md_path = task[4]
            try:
                status, path = future.result()
                if status == 'saved':
                    saved += 1
                elif status == 'skip':
                    skipped += 1
                log(f'{status:5s} {path}')
            except Exception as exc:
                errors.append((md_path, exc))
                log(f'ERROR {md_path}: {exc}')

    if errors:
        log(f'\nЗавершено с ошибками: {len(errors)} из {len(tasks)}')
        for path, exc in errors:
            log(f'  {path}: {exc}')
        return 1

    log(f'\nГотово. Сохранено: {saved}, пропущено: {skipped}, ошибок: 0.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log('Прервано пользователем.')
        sys.exit(130)
    except requests.RequestException as exc:
        log(f'Ошибка запроса к Stepik: {exc}')
        sys.exit(1)
