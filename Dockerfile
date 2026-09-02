FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sentinel_loader ./sentinel_loader
COPY samples ./samples
COPY pyproject.toml README.md ./
EXPOSE 8080
CMD ["python", "-m", "sentinel_loader", "--host", "0.0.0.0", "--port", "8080"]
