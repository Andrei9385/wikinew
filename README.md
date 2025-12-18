# Internal IT Wiki

Вики без базы данных на FastAPI с файловым хранилищем `/opt/wiki/content`. Интерфейс dark-only в стиле VMware, редактор markdown с предпросмотром и поддержкой Mermaid.

## Требования
- Debian 12
- Python 3.11+

## Установка
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск в разработке
```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

После старта приложение автоматически создаст `/opt/wiki/content` и базовые демоданные, если каталог пуст.

## Запуск через systemd (опционально)
1. Создайте юнит `/etc/systemd/system/internal-wiki.service`:
```ini
[Unit]
Description=Internal IT Wiki
After=network.target

[Service]
WorkingDirectory=/workspace/wikinew
ExecStart=/workspace/wikinew/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
2. Перезагрузите конфигурацию и запустите сервис:
```bash
systemctl daemon-reload
systemctl enable --now internal-wiki.service
```

## Структура хранилища
- Каждый узел — папка с `meta.json` и `index.md` (для сервисов дополнительно создаются файлы вкладок).
- В корне разрешены только компании, в компаниях — только площадки (DC), внутри DC/Section — разделы и объекты (Document/Service/Server/Network).
- Вложения сохраняются в подпапке `assets/` узла.

## Базовые URL
- `/` — дашборд компаний и недавних изменений.
- `/view/{path}` — просмотр узла и вкладок сервиса.
- `/edit/{path}` — редактор markdown с предпросмотром.
- `/search?q=` — полнотекстовый поиск по заголовкам и контенту.
- `/api/*` — операции создания, сохранения и загрузки файлов.
