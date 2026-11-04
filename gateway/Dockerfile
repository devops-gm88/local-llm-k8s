FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# --chmod: the build context comes from a checkout, and its file modes must not
# leak into the image (a 0600 app.py breaks the non-root container).
COPY --chmod=644 requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chmod=644 app.py .

# Non-root, and the Deployment also sets readOnlyRootFilesystem.
RUN useradd --uid 10001 --create-home appuser
USER 10001

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-access-log", "--log-level", "warning"]
