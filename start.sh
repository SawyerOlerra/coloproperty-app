#!/bin/bash
exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --timeout 120 --workers 1 app:app
