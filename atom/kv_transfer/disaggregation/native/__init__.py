# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""Single-node (XGMI) scale-up KV-transfer primitives."""

from atom.kv_transfer.disaggregation.native.vmm import VmmBuffer, supported

__all__ = ["VmmBuffer", "supported"]
