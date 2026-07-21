from __future__ import annotations

import unittest

from app.server import _instance_lock_path


class ServerInstanceTests(unittest.TestCase):
    def test_instance_lock_is_scoped_to_port(self) -> None:
        self.assertEqual(_instance_lock_path(8765).name, ".anishelf.8765.lock")
        self.assertNotEqual(_instance_lock_path(8765), _instance_lock_path(8877))


if __name__ == "__main__":
    unittest.main()
