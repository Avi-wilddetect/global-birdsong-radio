# FILE: Dockerfile
# A simplified and direct Dockerfile to eliminate PATH issues.

# Step 1: Start from the official Python base image.
FROM python:3.11-slim

# Step 2: Set the working directory inside the container.
WORKDIR /app

# Step 3: Copy and install the dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 4: Copy the rest of the application code.
COPY . .

# Step 5: Expose the application's port.
EXPOSE 5000

# Step 6: Run Gunicorn as a Python module to guarantee it is found.
# This avoids all PATH environment variable issues.
CMD ["python", "-m", "gunicorn", "-c", "gunicorn_config.py", "api_server:app"]