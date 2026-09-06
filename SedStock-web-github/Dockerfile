FROM python:3.12-slim

# exiftool (вписывает метаданные в фото) + его perl-зависимости.
# Именно из-за него нужен Docker — так exiftool ставится на сервер железно.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# exiftool лежит в PATH после apt-install
ENV EXIFTOOL_BIN=exiftool

# Render сам передаёт порт в переменную $PORT.
# 1 воркер (память) + несколько потоков (параллельные пользователи),
# большой таймаут — анализ пачки фото занимает время.
CMD gunicorn --workers 1 --threads 4 --timeout 600 --bind 0.0.0.0:$PORT sedstock_web:app
