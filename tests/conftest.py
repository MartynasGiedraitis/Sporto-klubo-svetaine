"""Set integration-test connections before application modules are collected."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true")
os.environ.setdefault("JWT_SECRET", "integration-test-secret-with-more-than-32-characters")
os.environ.setdefault("CLICKHOUSE_URL", "http://localhost:8123")
