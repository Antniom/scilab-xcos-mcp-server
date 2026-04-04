import sys
import unittest
from unittest.mock import patch

from tools import remote_hf_smoke_test


class RemoteHfSmokeTestTests(unittest.TestCase):
    def test_failed_validation_is_strict_by_default(self):
        validation = {
            "success": False,
            "code": "SCILAB_IMPORT_FAILED",
            "message": "Scilab import failed.",
        }

        with self.assertRaises(RuntimeError):
            remote_hf_smoke_test.ensure_validation_succeeded(validation)

    def test_successful_validation_passes(self):
        validation = {
            "success": True,
            "code": "OK",
            "validation_profile": "hosted_smoke",
        }

        remote_hf_smoke_test.ensure_validation_succeeded(validation)

    def test_cli_defaults_to_hosted_smoke(self):
        with patch.object(sys, "argv", ["remote_hf_smoke_test.py"]):
            args = remote_hf_smoke_test.parse_args()

        self.assertEqual(args.validation_profile, "hosted_smoke")

    def test_cli_allows_full_runtime_profile(self):
        with patch.object(sys, "argv", ["remote_hf_smoke_test.py", "--validation-profile", "full_runtime"]):
            args = remote_hf_smoke_test.parse_args()

        self.assertEqual(args.validation_profile, "full_runtime")


if __name__ == "__main__":
    unittest.main()
