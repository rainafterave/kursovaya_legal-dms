"""Фоновая проверка файлов: Redis → worker → PostgreSQL."""
import hashlib
import logging
from pathlib import Path
import signal
import time

import psycopg
import redis
from app import FILES, QUEUE, broker, db

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
running = True


def stop(*_):
    global running
    running = False


def check_file(file_id):
    with db() as conn:
        file = conn.execute("SELECT * FROM files WHERE id=%s AND status='pending' FOR UPDATE", (file_id,)).fetchone()
        if not file:
            return
        try:
            with (FILES / file['storage_key']).open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            status = 'ok' if digest == file['sha256'] else 'error'
            message = 'Контрольная сумма совпадает' if status == 'ok' else 'Контрольная сумма не совпадает'
        except OSError:
            status, message = 'error', 'Файл недоступен'
        conn.execute('UPDATE files SET status=%s,message=%s WHERE id=%s', (status, message, file_id))
    logging.info('Документ %s: %s', file_id, status)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_recovery = 0
    while running:
        try:
            # БД сохраняет pending, поэтому задания восстанавливаются после сбоя очереди.
            if time.monotonic() - last_recovery >= 30:
                with db() as conn:
                    pending = conn.execute("SELECT id FROM files WHERE status='pending' ORDER BY id LIMIT 100").fetchall()
                if pending:
                    broker.lpush(QUEUE, *(str(file['id']) for file in pending))
                last_recovery = time.monotonic()
            item = broker.brpop(QUEUE, timeout=5)
            if item:
                try:
                    check_file(int(item[1]))
                except ValueError:
                    logging.warning('Некорректный номер задания')
            Path('/tmp/worker-heartbeat').write_text(str(time.time()), encoding='ascii')
        except (psycopg.Error, redis.RedisError, OSError):
            logging.exception('Нет подключения; повтор через 3 секунды')
            last_recovery = 0
            time.sleep(3)


if __name__ == '__main__':
    main()
