web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn AZLive.wsgi:application --bind 0.0.0.0:$PORT --workers 1 --timeout 120
