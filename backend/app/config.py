import os
from dotenv import load_dotenv

load_dotenv()
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/pulse?replicaSet=rs0")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "pulse")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
JWT_SECRET = os.getenv("JWT_SECRET", "")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:8080")
PRODUCTION = os.getenv("ENVIRONMENT", "development") == "production"

def validate_configuration():
    if len(JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 characters.")
