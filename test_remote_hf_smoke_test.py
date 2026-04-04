import unittest

from tools import remote_hf_smoke_test


class RemoteHfSmokeTestTests(unittest.TestCase):
    def test_runtime_timeout_is_strict_by_default(self):
        validation = {
            "success": False,
            "code": "SCILAB_RUNTIME_TIMEOUT",
            "debug": {"structural_check": {"success": True}},
        }

        with self.assertRaises(RuntimeError):
            remote_hf_smoke_test.ensure_validation_succeeded(validation, allow_degraded_runtime=False)

    def test_runtime_timeout_can_be_allowed_explicitly(self):
        validation = {
            "success": False,
            "code": "SCILAB_RUNTIME_TIMEOUT",
            "debug": {"structural_check": {"success": True}},
        }

        degraded = remote_hf_smoke_test.ensure_validation_succeeded(validation, allow_degraded_runtime=True)
        self.assertTrue(degraded)


if __name__ == "__main__":
    unittest.main()
