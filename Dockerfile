FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx pydantic python-dotenv pyyaml
COPY . .
EXPOSE 8443
CMD ["python", "main.py"]
