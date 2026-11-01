FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY student/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY student/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
