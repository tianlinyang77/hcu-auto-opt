import unittest

from dcuopt.storage.postgres_queue import claim_jobs_sql


class PostgresQueueContractTests(unittest.TestCase):
    def test_claim_uses_skip_locked(self) -> None:
        sql = claim_jobs_sql(2)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("LIMIT 2", sql)

    def test_invalid_claim_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            claim_jobs_sql(0)


if __name__ == "__main__":
    unittest.main()

