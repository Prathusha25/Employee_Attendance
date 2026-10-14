"""Test configuration.

The app reads MONGO_URI / MONGO_DB at import time, so they are set here *before* app.main is imported.

* Unit and validation tests need no database.
* Integration tests run only when MONGO_TEST_URI is set (e.g. mongodb://localhost:27017). They use a throw-away
  database named hrone_test_<random> and drop it afterwards, so they never touch real data.
"""
import os
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MONGO_TEST_URI = os.environ.get("MONGO_TEST_URI")
TEST_DB_NAME = "hrone_test_" + uuid.uuid4().hex[:10]

# Real environment variables win over .env (load_dotenv(override=False)), so these take effect.
os.environ["MONGO_URI"] = MONGO_TEST_URI or "mongodb://127.0.0.1:1"  # unreachable port when no test DB
os.environ["MONGO_DB"] = TEST_DB_NAME
