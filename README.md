# Архив документов юридической фирмы

Учебный проект по дисциплине «Настройка и администрирование сервисного программного обеспечения». Тема: «Настройка и администрирование системы управления документами и файлами в крупных юридических фирмах».

## Запуск

Запустите Docker Desktop в режиме Linux containers. Откройте PowerShell в папке проекта:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Скрипт создаёт `.env`, собирает приложение и запускает **пять контейнеров**. На Windows устанавливать Python и PostgreSQL не нужно. Для первой сборки нужен интернет.

Сайт: **http://localhost:8080**. Учебный вход: **admin / Admin2026!**. Пароль задан в `.env` как `ADMIN_PASSWORD`. Адрес доступен только с этого компьютера.

Повторный запуск: `docker compose up -d --wait`. После изменения кода: `docker compose up -d --build --wait`. Остановка: `docker compose down`.

## Возможности

Одна страница: добавление дел клиентов, загрузка документов, поиск по названию или имени файла, фильтр по делу и скачивание. После загрузки worker проверяет целостность файла, результат появляется в таблице после нажатия «Обновить список».

Поддерживаются PDF, DOC/DOCX, XLS/XLSX, TXT, RTF, ODT, PNG и JPG до 25 МБ. Файлы сохраняются под случайными именами в Docker-томе; сведения о них — в PostgreSQL. Перед скачиванием проверяется SHA-256. В рабочей БД две таблицы: `matters` и `files`.

## Контейнеры

| Сервис | Задача |
| --- | --- |
| nginx | Вход на сайт через порт 8080, перенаправление запросов к приложению |
| app | Собственное приложение Python/Flask: каталог, загрузка, поиск, скачивание |
| db | PostgreSQL: дела и сведения о документах |
| redis | Очередь заданий на проверку файлов |
| worker | Отдельный Python-процесс: получает задания и проверяет файлы |

Все контейнеры находятся в сети `legal-network` и обращаются друг к другу по именам сервисов. `app` и `worker` используют общий образ с разными командами запуска. Общий файловый том подключён к worker только для чтения.

```text
Браузер → nginx → app → PostgreSQL
                  ↓        ↑
                Redis → worker
```

Тома `postgres-data`, `document-files` и `redis-data` сохраняют данные после остановки и пересоздания контейнеров. Незавершённые задания дополнительно хранятся в БД: worker восстанавливает их после сбоя очереди каждые 30 секунд.

## Команды для демонстрации

```powershell
docker compose ps
docker compose logs --tail 20 app worker
docker network inspect legal-dms_legal-network
docker compose exec redis redis-cli PING
docker compose exec redis redis-cli LLEN legal:files
docker compose exec db psql -U legal_app -d legal_archive -c "SELECT id,title,status FROM files;"
Invoke-RestMethod http://localhost:8080/healthz
```

Чтобы показать очередь: выполните `docker compose stop worker`, загрузите документ и проверьте статус «В очереди». Затем выполните `docker compose start worker` и через несколько секунд обновите список: появится «Проверен».

## Резервная копия

```powershell
powershell -ExecutionPolicy Bypass -File .\backup.ps1
```

Скрипт временно останавливает приложение, Nginx и worker, сохраняет БД, файлы и `.env` в `backups/дата_время/`, затем запускает сервисы. Файл `COMPLETE` означает успешное копирование. Копия Redis не нужна: очередь восстанавливается по статусам БД. Сохраняйте копии в закрытом месте, поскольку `.env` содержит секреты.

Восстановление заменяет данные рабочей БД выбранной копией. Сначала сохраните текущее состояние. Подставьте свой путь и выполняйте команды последовательно; при ошибке остановитесь:

```powershell
$backupDir = (Resolve-Path '.\backups\дата_время').Path
docker compose stop nginx worker app
docker compose cp "$backupDir\database.dump" db:/tmp/restore.dump
docker compose exec -T db pg_restore -U legal_app -d legal_archive --clean --if-exists --no-owner --exit-on-error /tmp/restore.dump
docker compose run --rm --no-deps -T --volume "${backupDir}:/backup:ro" --entrypoint tar app -xzf /backup/files.tar.gz -C /data/files
docker compose exec -T redis redis-cli DEL legal:files
docker compose exec -T db rm -f /tmp/restore.dump
docker compose up -d --wait
```

Не удаляйте действующий `.env`: пароль БД должен соответствовать её тому. `docker compose down -v` удаляет все тома и данные; для обычной остановки ключ `-v` не используется.

## Файлы проекта

`docker-compose.yml` описывает пять сервисов, сеть и тома. `Dockerfile` собирает Python-образ. В `app/` находятся `app.py`, `worker.py`, `schema.sql`, две HTML-страницы в `templates/` и один файл стилей в `static/`. В `nginx/nginx.conf` находится настройка прокси.

Проект рассчитан на учебную демонстрацию с вымышленными документами. Для реальной юридической фирмы потребуется отдельное проектирование доступа сотрудников и защиты данных.
