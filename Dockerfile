FROM python:3.13.0

# Установка Git
RUN apt-get update && apt-get install -y git

# Копирование файлов проекта
WORKDIR /app
COPY . .

# Установка зависимостей
RUN pip install --no-cache-dir -r requirements.txt

# Запуск бота
CMD ["python3", "bot1.py"]
