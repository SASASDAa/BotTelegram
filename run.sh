#!/bin/bash
# Скрипт для запуска/управления ботом с помощью Docker Compose

# Переходим в директорию, где находится этот скрипт.
cd "$(dirname "$0")"

# Проверяем, существует ли файл .env
if [ ! -f .env ]; then
    echo "Ошибка: Файл .env не найден."
    echo "Пожалуйста, скопируйте .env.example в .env и заполните его вашими данными."
    exit 1
fi

# Функция для вывода справки
show_help() {
    echo "Использование: ./run.sh [команда]"
    echo ""
    echo "Команды:"
    echo "  up        Собрать и запустить контейнеры в фоновом режиме."
    echo "  down      Остановить и удалить контейнеры."
    echo "  logs      Показать логи бота (нажмите Ctrl+C для выхода)."
    echo "  restart   Перезапустить сервисы."
    echo "  build     Пересобрать образы без запуска."
    echo "  help      Показать это сообщение."
    echo ""
    echo "Если команда не указана, по умолчанию будет выполнена 'up'."
}

# Основная логика скрипта
case "$1" in
    up)
        echo "Сборка и запуск контейнеров..."
        docker compose up --build -d
        ;;
    down)
        echo "Остановка и удаление контейнеров..."
        docker compose down
        ;;
    logs)
        echo "Отслеживание логов бота... (Нажмите Ctrl+C для выхода)"
        docker compose logs -f bot
        ;;
    restart)
        echo "Перезапуск сервисов..."
        docker compose restart
        ;;
    build)
        echo "Пересборка образов..."
        docker compose build
        ;;
    help|*)
        show_help
        ;;
esac

echo "Готово."
