import os
from functools import lru_cache


@lru_cache(maxsize=1)
def torch_cuda_available() -> bool:
    try:
        import torch

        if not torch.cuda.is_available():
            return False
        probe = torch.ones((1,), device="cuda")
        _ = probe + 1
        torch.cuda.synchronize()
        return True
    except Exception:
        return False


def resolve_torch_device() -> str:
    configured = os.environ.get("SOULDRIVE_TORCH_DEVICE", "auto").strip().lower()
    if configured in ("", "auto"):
        return "cuda" if torch_cuda_available() else "cpu"
    if configured.startswith("cuda") and not torch_cuda_available():
        return "cpu"
    return configured
