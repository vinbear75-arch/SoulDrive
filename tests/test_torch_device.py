import unittest
from unittest.mock import patch

from core.torch_device import resolve_torch_device


class TorchDeviceTests(unittest.TestCase):
    def test_auto_prefers_cuda_when_available(self):
        with patch.dict("os.environ", {}, clear=True), patch(
            "core.torch_device.torch_cuda_available",
            return_value=True,
        ):
            self.assertEqual(resolve_torch_device(), "cuda")

    def test_auto_falls_back_to_cpu_when_cuda_is_unavailable(self):
        with patch.dict("os.environ", {}, clear=True), patch(
            "core.torch_device.torch_cuda_available",
            return_value=False,
        ):
            self.assertEqual(resolve_torch_device(), "cpu")

    def test_explicit_cpu_disables_cuda(self):
        with patch.dict("os.environ", {"SOULDRIVE_TORCH_DEVICE": "cpu"}), patch(
            "core.torch_device.torch_cuda_available",
            return_value=True,
        ):
            self.assertEqual(resolve_torch_device(), "cpu")

    def test_explicit_cuda_falls_back_when_unavailable(self):
        with patch.dict("os.environ", {"SOULDRIVE_TORCH_DEVICE": "cuda"}), patch(
            "core.torch_device.torch_cuda_available",
            return_value=False,
        ):
            self.assertEqual(resolve_torch_device(), "cpu")


if __name__ == "__main__":
    unittest.main()
