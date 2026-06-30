FROM python:3.11-alpine
RUN apk add --no-cache gcc musl-dev postgresql-dev libffi-dev
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 3003
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3003"]
