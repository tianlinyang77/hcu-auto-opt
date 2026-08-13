import os
import unittest
from uuid import uuid4

from dcuopt.storage.postgres_queue import POSTGRES_SCHEMA, claim_jobs_sql

try:
    import psycopg
except ImportError:  # pragma: no cover - optional until dev dependencies are installed
    psycopg = None


DATABASE_URL = os.getenv("DCUOPT_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
class PostgresClaimIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        assert psycopg is not None
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute(POSTGRES_SCHEMA)
            cursor.execute("TRUNCATE jobs")
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def test_claim_is_atomic_and_returns_distinct_jobs(self) -> None:
        job_ids = [uuid4(), uuid4()]
        with self.connection.cursor() as cursor:
            for job_id in job_ids:
                cursor.execute(
                    "INSERT INTO jobs (job_id, job_type, payload) VALUES (%s, %s, %s)",
                    (job_id, "fixture", "{}"),
                )
        self.connection.commit()

        claimed: list[str] = []
        for worker in ("worker-a", "worker-b"):
            with self.connection.cursor() as cursor:
                cursor.execute(
                    claim_jobs_sql(),
                    {"worker_id": worker, "claim_token": uuid4()},
                )
                claimed.append(str(cursor.fetchone()[0]))
            self.connection.commit()

        self.assertEqual(len(set(claimed)), 2)
