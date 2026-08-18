import unittest

from hcuopt.storage.postgres_queue import claim_for_worker_sql, claim_jobs_sql


class PostgresQueueContractTests(unittest.TestCase):
    def test_claim_uses_skip_locked(self) -> None:
        sql = claim_jobs_sql(2)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("LIMIT 2", sql)

    def test_invalid_claim_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            claim_jobs_sql(0)

    def test_worker_claim_matches_explicit_adapter_profile(self) -> None:
        sql = claim_for_worker_sql()
        self.assertIn("accepted_worker_type = %(worker_type)s", sql)
        self.assertIn("adapter_profile = %(adapter_profile)s", sql)


if __name__ == "__main__":
    unittest.main()
