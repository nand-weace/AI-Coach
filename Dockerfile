FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Planet positions for the Career Horoscope birth chart (birth_chart.py).
# Fetched at build time so the app never downloads it while serving.
ENV EPHEMERIS_DIR=/opt/ephemeris
RUN python -c "from skyfield.api import Loader; Loader('/opt/ephemeris', verbose=False)('de421.bsp')"

COPY . .

ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
