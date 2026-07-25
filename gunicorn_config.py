# FILE: gunicorn_config.py
# This file contains the configuration for the Gunicorn production server.

# The host and port to bind to.
bind = "0.0.0.0:5000"

# The number of worker processes.
workers = 3

# The type of worker to use.
worker_class = "sync"

# Loglevel.
loglevel = "info"

# Log locations
accesslog = "-"
errorlog = "-"

# --- THE FIX ---
# Explicitly tell Gunicorn where the Flask 'app' object is.
# Format: "filename:variable_name"
app_uri = "api_server:app"