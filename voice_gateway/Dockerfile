FROM python:3.12-slim

WORKDIR /app

COPY voice_gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY voice_gateway/ ./voice_gateway/

EXPOSE 8080

CMD ["uvicorn", "voice_gateway.api.app:create_app", "--host", "0.0.0.0", "--port", "8080", "--factory"]
