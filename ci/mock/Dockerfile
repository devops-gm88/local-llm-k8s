FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY mock_ollama.py .
EXPOSE 11434
CMD ["python3", "/app/mock_ollama.py"]
