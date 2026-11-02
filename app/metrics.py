"""Метрики сайта: общие для процессов Gunicorn счётчики хранятся в Redis."""
import json
import os
import time
import uuid

import psycopg
import redis
from flask import Response, g, request, session

PREFIX = os.getenv('METRICS_PREFIX', 'legal:metrics')
COUNTERS = PREFIX + ':counters'
SESSIONS = PREFIX + ':sessions'
ACTIVE_SECONDS = 300
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
SOURCES = ('site', 'api')
ACTIONS = ('search', 'upload', 'download', 'create_matter', 'logout')
DEFINITIONS = {
    'requests': ('legal_http_requests_total', ('method', 'route', 'status', 'source'), 'Application HTTP responses; infrastructure requests excluded.'),
    'page_views': ('legal_page_views_total', ('page',), 'Successful GET views of the login and archive pages.'),
    'logins': ('legal_logins_total', ('result',), 'Website login attempts with a valid CSRF token.'),
    'actions': ('legal_user_actions_total', ('action', 'source'), 'Successful document operations, searches and logouts.'),
    'transfer_bytes': ('legal_file_transferred_bytes_total', ('direction', 'source'), 'Bytes of saved uploads or files prepared for download; not network delivery confirmation.'),
}


def field(name, *labels):
    return json.dumps([name, *labels], separators=(',', ':'))


def record_action(name, size=0):
    """Вызывать после успешной операции, не сохраняя названия файлов и клиентов."""
    g.metric_actions = getattr(g, 'metric_actions', []) + [(name, size)]
    if name == 'logout':
        g.retired_session = session.get('activity_id')


def record_login(success):
    g.metric_login = 'success' if success else 'failure'
    if success:
        g.retired_session = session.get('activity_id')


def sample(name, value, **labels):
    suffix = '{' + ','.join(f'{key}={json.dumps(str(val))}' for key, val in labels.items()) + '}' if labels else ''
    return f'{name}{suffix} {value}'


def defaults():
    values = {field('page_views', page): '0' for page in ('login', 'archive')}
    values.update({field('logins', result): '0' for result in ('success', 'failure')})
    for source in SOURCES:
        for action in ACTIONS:
            values[field('actions', action, source)] = '0'
        for direction in ('upload', 'download'):
            values[field('transfer_bytes', direction, source)] = '0'
        for name in ('duration_count', 'duration_us'):
            values[field(name, source)] = '0'
        for bucket in (*BUCKETS, '+Inf'):
            values[field('duration_bucket', source, str(bucket))] = '0'
    return values


def register_metrics(app, broker, db):
    @app.before_request
    def start_request():
        endpoint = request.endpoint or ''
        if endpoint in ('health', 'api.health', 'static', 'metrics', 'api.openapi') or request.path.startswith('/api/docs'):
            return
        g.metric_start = time.perf_counter()

    @app.after_request
    def finish_request(response):
        if not hasattr(g, 'metric_start'):
            return response
        source = 'api' if request.path.startswith('/api/') else 'site'
        # Шаблон маршрута вместо URL: номера документов и запросы поиска не попадают в labels.
        route = request.url_rule.rule if request.url_rule else 'unmatched'
        method = request.method if request.method in ('GET', 'POST', 'HEAD', 'OPTIONS', 'PUT', 'PATCH', 'DELETE') else 'OTHER'
        elapsed = max(0, time.perf_counter() - g.metric_start)
        now = time.time()
        pipe = broker.pipeline()

        def increment(name, *labels, amount=1):
            pipe.hincrby(COUNTERS, field(name, *labels), amount)

        increment('requests', method, route, str(response.status_code), source)
        increment('duration_count', source)
        increment('duration_us', source, amount=round(elapsed * 1_000_000))
        for bucket in BUCKETS:
            if elapsed <= bucket:
                increment('duration_bucket', source, str(bucket))
        increment('duration_bucket', source, '+Inf')
        if hasattr(g, 'metric_login'):
            increment('logins', g.metric_login)
        if response.status_code < 400:
            if request.method == 'GET' and response.status_code == 200 and request.endpoint in ('index', 'login'):
                increment('page_views', 'archive' if request.endpoint == 'index' else 'login')
            if response.status_code == 200 and request.method == 'GET' and request.endpoint in ('index', 'api.files') and (
                    request.args.get('q', '').strip() or request.args.get('matter', type=int) is not None):
                increment('actions', 'search', source)
            for action, size in getattr(g, 'metric_actions', []):
                if request.method == 'HEAD':
                    continue
                increment('actions', action, source)
                if action in ('upload', 'download'):
                    increment('transfer_bytes', action, source, amount=size)
        if getattr(g, 'retired_session', None):
            pipe.zrem(SESSIONS, g.retired_session)
        pipe.zremrangebyscore(SESSIONS, '-inf', now - ACTIVE_SECONDS)
        if source == 'site' and request.endpoint and session.get('logged_in') and response.status_code < 400:
            activity_id = session.setdefault('activity_id', uuid.uuid4().hex)
            pipe.zadd(SESSIONS, {activity_id: now})
            pipe.expire(SESSIONS, ACTIVE_SECONDS * 2)
        try:
            pipe.execute()
        except redis.RedisError:
            # Отказ учёта метрик не должен мешать пользователю работать с документами.
            app.logger.warning('Не удалось записать метрики запроса в Redis')
        return response

    @app.get('/metrics')
    def metrics():
        try:
            pipe = broker.pipeline()
            pipe.zremrangebyscore(SESSIONS, '-inf', time.time() - ACTIVE_SECONDS)
            pipe.hgetall(COUNTERS)
            pipe.zcard(SESSIONS)
            _, stored, active = pipe.execute()
        except redis.RedisError:
            return Response('# Metrics storage unavailable\n', status=503, mimetype='text/plain')
        values = defaults()
        values.update(stored)
        parsed = [(json.loads(key), value) for key, value in values.items()]
        lines = []
        for internal, (name, labels, help_text) in DEFINITIONS.items():
            lines += [f'# HELP {name} {help_text}', f'# TYPE {name} counter']
            for (kind, *label_values), value in parsed:
                if kind == internal:
                    lines.append(sample(name, value, **dict(zip(labels, label_values))))
        duration = 'legal_http_request_duration_seconds'
        lines += [f'# HELP {duration} Server processing time until response headers; excludes body transfer.',
                  f'# TYPE {duration} histogram']
        for source in SOURCES:
            for bucket in (*BUCKETS, '+Inf'):
                lines.append(sample(duration + '_bucket', values[field('duration_bucket', source, str(bucket))], source=source, le=bucket))
            lines.append(sample(duration + '_count', values[field('duration_count', source)], source=source))
            lines.append(sample(duration + '_sum', int(values[field('duration_us', source)]) / 1_000_000, source=source))
        lines += ['# HELP legal_active_sessions Authenticated browser sessions active in the last 300 seconds; not unique people.',
                  '# TYPE legal_active_sessions gauge', sample('legal_active_sessions', active)]
        try:
            with db() as conn:
                documents = conn.execute('SELECT status, COUNT(*) AS count, COALESCE(SUM(size_bytes),0) AS bytes FROM files GROUP BY status').fetchall()
                matters = conn.execute('SELECT COUNT(*) AS count FROM matters').fetchone()['count']
            counts = {row['status']: row['count'] for row in documents}
            lines += ['# HELP legal_archive_documents Current documents by integrity check status.',
                      '# TYPE legal_archive_documents gauge']
            for status in ('pending', 'ok', 'error'):
                lines.append(sample('legal_archive_documents', counts.get(status, 0), status=status))
            lines += ['# HELP legal_archive_matters Current matters in the database.',
                      '# TYPE legal_archive_matters gauge', sample('legal_archive_matters', matters),
                      '# HELP legal_archive_storage_bytes Total document size recorded in the database.',
                      '# TYPE legal_archive_storage_bytes gauge', sample('legal_archive_storage_bytes', sum(row['bytes'] for row in documents))]
            available = 1
        except psycopg.Error:
            available = 0
        lines += ['# HELP legal_archive_available Whether the archive database snapshot could be collected.',
                  '# TYPE legal_archive_available gauge', sample('legal_archive_available', available)]
        return Response('\n'.join(lines) + '\n', content_type='text/plain; version=0.0.4; charset=utf-8')
