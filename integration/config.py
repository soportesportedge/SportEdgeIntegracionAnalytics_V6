import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def secret(name):
    path = os.getenv(name + '_FILE')
    return Path(path).read_text().strip() if path else os.getenv(name, '')

def enabled(name): return os.getenv(name, 'false').lower() == 'true'

def bounded(name, default, low, high):
    return max(low, min(high, float(os.getenv(name, str(default)))))

def database_url():
    return os.getenv('V6_DATABASE_URL','postgresql://v6@v6-db:5432/sportedge_v6')

def model_url(): return os.getenv('V6_MODEL_URL','http://analytics:8788').rstrip('/')
