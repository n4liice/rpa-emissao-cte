FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY . .

RUN mkdir -p data/xml_recebidos logs

EXPOSE 8000

CMD ["python", "main.py"]
