# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""HIP VMM based cross-process GPU buffer sharing (scale-up KV transfer).

Foundation for a single-node (XGMI) prefill/decode KV connector. A producer
allocates an exportable VMM buffer, shares its POSIX file descriptor over a
UNIX socket (``socket.send_fds``); a consumer imports it, grants its own device
peer access and copies directly over the fabric with a plain ``Tensor.copy_``
(``hipMemcpyPeerAsync``). Unlike legacy hipIpc, VMM shareable handles work
reliably across processes and do not require the source GPU to be visible to
the consumer.

The tiny C++ helper is JIT-compiled on first use (never at import time), so
importing this module is cheap and safe on CPU-only hosts.
"""

from __future__ import annotations

import functools
import os

import torch

__all__ = ["supported", "VmmBuffer"]


@functools.lru_cache(maxsize=1)
def _ext():
    from torch.utils.cpp_extension import load

    rocm = os.environ.get("ROCM_PATH", "/opt/rocm")
    src = os.path.join(os.path.dirname(__file__), "_vmm_ext.cpp")
    return load(
        name="atom_vmm_ext",
        sources=[src],
        extra_include_paths=[os.path.join(rocm, "include")],
        extra_ldflags=[f"-L{os.path.join(rocm, 'lib')}", "-lamdhip64"],
        verbose=False,
    )


def supported(device: int = 0) -> bool:
    """Whether the device supports HIP Virtual Memory Management."""
    return bool(_ext().vmm_supported(device))


class VmmBuffer:
    """An exportable VMM buffer mapped on one device.

    Create with :meth:`alloc` (producer) or :meth:`import_fd` (consumer). Use
    :meth:`tensor` to get a (non-owning) view for reads/writes/copies.
    """

    def __init__(self, region_id: int, nbytes: int, device: int):
        self._id = region_id
        self.nbytes = nbytes
        self.device = device

    @classmethod
    def alloc(cls, nbytes: int, device: int) -> "VmmBuffer":
        return cls(_ext().vmm_alloc(nbytes, device), nbytes, device)

    @classmethod
    def import_fd(cls, fd: int, nbytes: int, device: int) -> "VmmBuffer":
        """Import a producer's exported fd and map it on ``device``.

        ``nbytes`` must match the producer's ``alloc`` size.
        """
        return cls(_ext().vmm_import(fd, nbytes, device), nbytes, device)

    def export_fd(self) -> int:
        """POSIX fd to send to a peer (e.g. via ``socket.send_fds``)."""
        return _ext().vmm_export_fd(self._id)

    def tensor(self, dtype: torch.dtype, shape) -> torch.Tensor:
        """View of the buffer as ``dtype`` reshaped to ``shape``.

        The returned tensor keeps this :class:`VmmBuffer` (and therefore the
        underlying VMM mapping) alive for its own lifetime, so callers may
        drop the buffer reference and keep only the tensor.
        """
        flat = _ext().vmm_tensor(self._id, self.nbytes, self.device)  # uint8
        view = flat.view(dtype).view(*shape)
        view._vmm_keepalive = self  # tie mapping lifetime to the tensor
        return view

    def close(self) -> None:
        if self._id is not None:
            _ext().vmm_free(self._id)
            self._id = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
