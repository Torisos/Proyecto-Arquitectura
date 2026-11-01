FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY auth/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY auth/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
