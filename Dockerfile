FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

ENV PRINTER_HOST=172.23.24.79 \
    PRINTER_PORT=9100 \
    DATA_DIR=/data \
    PORT=8080

VOLUME /data
EXPOSE 8080

CMD ["python", "app.py"]
