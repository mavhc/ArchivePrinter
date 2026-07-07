FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARCHIVE_ROOT=/archive \
    ARCHIVE_PRINTER_CONFIG=/config/config.json \
    PORT=8631

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY archive_printer ./archive_printer

RUN useradd --system --create-home --home-dir /home/archive-printer archive-printer \
    && mkdir -p /archive /config \
    && chown -R archive-printer:archive-printer /archive /config

VOLUME ["/archive", "/config"]
EXPOSE 8631

USER archive-printer
CMD ["python", "-m", "archive_printer.server"]
