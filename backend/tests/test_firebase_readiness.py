import re
import unittest
from unittest.mock import MagicMock

from app.firebase import FirestoreScanRepository


class FirestoreReadinessTests(unittest.TestCase):
    def test_healthcheck_uses_non_reserved_id_and_bounded_read_only_query(self):
        client = MagicMock()
        users = client.collection.return_value
        scans = users.document.return_value.collection.return_value
        scans.limit.return_value.stream.return_value = iter(())
        FirestoreScanRepository(client).healthcheck()
        client.collection.assert_called_once_with("users")
        probe_id = users.document.call_args.args[0]
        self.assertIsNone(re.fullmatch(r"__.*__", probe_id))
        scans.limit.assert_called_once_with(1)
        scans.limit.return_value.stream.assert_called_once_with(timeout=3, retry=None)
        users.document.return_value.set.assert_not_called()
        users.document.return_value.delete.assert_not_called()
