FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && \
    apt-get install -y default-jre procps && \
    apt-get clean

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
