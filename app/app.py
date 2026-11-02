"""Простой каталог документов юридической фирмы."""
import hashlib
import hmac
import os
from pathlib import Path
import sys
import uuid

import psycopg
from psycopg.rows import dict_row
import redis
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from metrics import record_action, record_login, register_metrics

app = Flask(__name__)
app.config.update(SECRET_KEY=os.environ['SECRET_KEY'], MAX_CONTENT_LENGTH=26 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
FILES = Path(os.getenv('FILES_DIR', '/data/files'))
QUEUE = os.getenv('TASK_QUEUE', 'legal:files')
broker = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'),
                             decode_responses=True, socket_connect_timeout=2, socket_timeout=7)
EXTENSIONS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.txt', '.rtf', '.odt', '.png', '.jpg', '.jpeg'}


def db():
    return psycopg.connect(host=os.getenv('DB_HOST', 'db'), dbname=os.getenv('DB_NAME', 'legal_archive'),
                           user=os.getenv('DB_USER', 'legal_app'), password=os.environ['DB_PASSWORD'],
                           row_factory=dict_row, connect_timeout=5)


def initialize():
    FILES.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        # Два экземпляра запускаются одновременно: создаём схему и примеры по очереди.
        conn.execute('SELECT pg_advisory_xact_lock(761204)')
        conn.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
        if not conn.execute('SELECT 1 FROM matters LIMIT 1').fetchone():
            for number, title, client in [('ЮФ-001', 'Договоры поставки', 'ООО «Север»'),
                                           ('ЮФ-002', 'Корпоративные документы', 'АО «Вектор»')]:
                conn.execute('INSERT INTO matters (number,title,client) VALUES (%s,%s,%s)', (number, title, client))


register_metrics(app, broker, db)


@app.before_request
def check_access():
    if request.path.startswith('/api/'):
        if request.path.startswith('/api/docs') or request.path in ('/api/openapi.json', '/api/health'):
            return
        auth = request.authorization
        valid = auth and auth.type == 'basic' and auth.username == 'admin' and hmac.compare_digest(
            (auth.password or '').encode(), os.environ['ADMIN_PASSWORD'].encode())
        if not valid:
            return jsonify(error='Требуется авторизация admin'), 401, {'WWW-Authenticate': 'Basic realm="Legal archive"'}
        return
    if request.endpoint in ('health', 'static', 'metrics'):
        return
    if request.endpoint != 'login' and not session.get('logged_in'):
        return redirect(url_for('login'))
    if request.method == 'POST' and not hmac.compare_digest(session.get('csrf', '').encode(), request.form.get('csrf', '!').encode()):
        abort(400, description='Форма устарела. Обновите страницу.')


@app.context_processor
def form_token():
    return {'csrf': session.setdefault('csrf', uuid.uuid4().hex)}


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password', '').encode('utf-8')
        if request.form.get('username') == 'admin' and hmac.compare_digest(password, os.environ['ADMIN_PASSWORD'].encode('utf-8')):
            record_login(True)
            session.clear()
            session['logged_in'] = True
            return redirect(url_for('index'))
        record_login(False)
        flash('Неверный логин или пароль.')
        return render_template('login.html'), 401
    return render_template('login.html')


@app.post('/logout')
def logout():
    record_action('logout')
    session.clear()
    return redirect(url_for('login'))


@app.get('/healthz')
def health():
    try:
        with db() as conn:
            conn.execute('SELECT 1')
        broker.ping()
        if not os.access(FILES, os.W_OK):
            raise OSError('storage unavailable')
        return {'status': 'ok', 'database': 'ok', 'queue': 'ok', 'files': 'ok'}
    except (psycopg.Error, redis.RedisError, OSError):
        return {'status': 'unavailable'}, 503


@app.get('/')
def index():
    q, matter_id = request.args.get('q', '').strip()[:150], request.args.get('matter', type=int)
    with db() as conn:
        matters = conn.execute('SELECT * FROM matters ORDER BY id').fetchall()
        files = conn.execute('''
            SELECT f.*, m.number, m.client FROM files f JOIN matters m ON m.id=f.matter_id
            WHERE (%s::INTEGER IS NULL OR f.matter_id=%s)
              AND (f.title ILIKE %s OR f.filename ILIKE %s)
            ORDER BY f.created_at DESC, f.id DESC
        ''', (matter_id, matter_id, f'%{q}%', f'%{q}%')).fetchall()
    return render_template('index.html', matters=matters, files=files, q=q, matter_id=matter_id)


@app.post('/matters')
def create_matter():
    number, title, client = (request.form.get(name, '').strip() for name in ('number', 'title', 'client'))
    if not (1 <= len(number) <= 30 and 1 <= len(title) <= 150 and 1 <= len(client) <= 150):
        abort(400, description='Заполните номер, название дела и клиента.')
    try:
        with db() as conn:
            row = conn.execute('INSERT INTO matters (number,title,client) VALUES (%s,%s,%s) RETURNING *', (number, title, client)).fetchone()
        record_action('create_matter')
        if request.path.startswith('/api/'):
            return jsonify(row), 201
        flash('Дело добавлено.')
    except psycopg.errors.UniqueViolation:
        if request.path.startswith('/api/'):
            return jsonify(error='Такой номер дела уже существует.'), 409
        flash('Такой номер дела уже существует.')
    return redirect(url_for('index'))


@app.post('/upload')
def upload():
    title, matter_id = request.form.get('title', '').strip(), request.form.get('matter', type=int)
    file = request.files.get('file')
    filename = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1] if file else ''
    if not (1 <= len(title) <= 150 and 1 <= len(filename) <= 200) or any(ord(c) < 32 for c in filename):
        abort(400, description='Укажите название документа и выберите файл.')
    if Path(filename).suffix.lower() not in EXTENSIONS:
        abort(400, description='Разрешены PDF, Word, Excel, TXT, RTF, ODT, PNG и JPG.')
    with db() as conn:
        if not conn.execute('SELECT id FROM matters WHERE id=%s', (matter_id,)).fetchone():
            abort(400, description='Выберите существующее дело.')
    key, digest, size = uuid.uuid4().hex, hashlib.sha256(), 0
    try:
        with (FILES / key).open('xb') as output:
            while chunk := file.stream.read(1024 * 1024):
                size += len(chunk)
                if size > 25 * 1024 * 1024:
                    abort(413, description='Файл должен быть не больше 25 МБ.')
                digest.update(chunk)
                output.write(chunk)
        if not size:
            abort(400, description='Нельзя загрузить пустой файл.')
        with db() as conn:
            file_id = conn.execute('''
                INSERT INTO files (matter_id,title,filename,storage_key,size_bytes,sha256)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            ''', (matter_id, title, filename, key, size, digest.hexdigest())).fetchone()['id']
    except BaseException:
        (FILES / key).unlink(missing_ok=True)
        raise
    try:
        broker.lpush(QUEUE, str(file_id))
    except redis.RedisError:
        app.logger.warning('Очередь недоступна; worker повторно поставит документ %s на проверку', file_id)
    record_action('upload', size)
    if request.path.startswith('/api/'):
        return jsonify(id=file_id, title=title, filename=filename, status='pending'), 201
    flash('Документ загружен. Обновите список через несколько секунд для результата проверки.')
    return redirect(url_for('index', matter=matter_id))


@app.get('/files/<int:file_id>/download')
def download(file_id):
    with db() as conn:
        file = conn.execute('SELECT * FROM files WHERE id=%s', (file_id,)).fetchone()
    if not file:
        abort(404)
    try:
        with (FILES / file['storage_key']).open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
    except OSError:
        abort(503, description='Файл недоступен в хранилище.')
    if digest != file['sha256']:
        abort(503, description='Нарушена целостность файла.')
    response = send_file(FILES / file['storage_key'], download_name=file['filename'], as_attachment=True,
                         mimetype='application/octet-stream', conditional=False)
    record_action('download', file['size_bytes'])
    return response


@app.after_request
def headers(response):
    response.headers['X-App-Instance'] = os.environ.get('HOSTNAME', 'app')
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


if __name__ == '__main__' and sys.argv[1:] == ['init-db']:
    initialize()
elif __name__ != '__main__':
    from api import register_api
    register_api(app)
