# TikTok Watcher → Telegram (GitHub Actions)

Следит за новыми видео указанных блогеров TikTok и присылает их в Telegram.
Работает на GitHub Actions: домашний ПК и сервер не нужны, оплата не нужна.

Проверка запускается по расписанию раз в 10 минут, состояние (`state.json`) хранится в репозитории,
поэтому одно и то же видео не приходит дважды.

## Настройка (3 шага)

### 1. Секреты

Settings → **Secrets and variables → Actions → New repository secret**:

| Имя | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен вашего бота от @BotFather |
| `TELEGRAM_CHAT_ID` | ваш chat_id (число) |

### 2. Список блогеров

По умолчанию в workflow: `kz_lemon4ik,chopocompik_off`.
Чтобы изменить — Settings → Secrets and variables → Actions → вкладка **Variables** →
New repository variable `BLOGGERS` = `ник1,ник2` (без `@`).

### 3. Запуск

Вкладка **Actions** → workflow `tiktok-watch` → **Run workflow**.
Первый запуск молча запоминает уже опубликованные видео (уведомлений не будет),
дальше приходят только новые.

## Что приходит в Telegram

```
🎬 Новое видео @kz_lemon4ik
<название видео>
https://www.tiktok.com/@kz_lemon4ik/video/...
```
и сам mp4-файл вложением.

## Полезно знать

- **Репозиторий должен быть публичным** — на приватных бесплатные минуты Actions ограничены
  (2000 мин/мес), а проверка каждые 10 минут съедает больше. Публичные репозитории — без лимита.
  Если репозиторий приватный, поменяйте cron на `*/30 * * * *` (раз в 30 минут).
- **Расписание может опаздывать** на 5–20 минут — это ограничение GitHub.
- **GitHub отключает scheduled workflows после 60 дней без активности** в репозитории;
  workflow сам раз в 3 дня коммитит `.keepalive`, чтобы этого не случилось.
- **Токен не хранится в коде** — только в Secrets. `config.json` в `.gitignore`.
- `yt-dlp` обновляется автоматически при каждом запуске (устанавливается свежий из PyPI).
- Если TikTok начнёт отдавать пустые ответы с IP-адресов GitHub, добавьте в workflow шаг
  с прокси или перенесите на VPS (см. папку `deploy/` в локальном проекте).

## Локальный запуск (для отладки)

```bash
pip install yt-dlp curl_cffi
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python tiktok_watch.py --once
```
