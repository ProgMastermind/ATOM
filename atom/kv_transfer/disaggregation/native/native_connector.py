# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""Native single-node KV connector (HIP VMM, no third-party transport).

A fully in-tree prefill/decode KV connector for the single-node (scale-up /
XGMI) case. It depends only on the HIP Virtual Memory Management API
(:mod:`atom.kv_transfer.disaggregation.native.vmm`) — no MoRI, no Mooncake.

Selected with ``--kv-transfer-config '{"kv_connector":"native", ...}'``.

Data path (push, producer -> consumer):
  * The consumer allocates a VMM "staging" buffer, exports its POSIX fd, and
    sends it (plus the request's destination block ids) to the producer over a
    UNIX side channel.
  * The producer imports the consumer's staging (granting its own device
    access), gathers the request's KV blocks straight into it over the fabric
    (``hipMemcpy`` peer, i.e. XGMI), and replies WRITE_DONE.
  * The consumer scatters from its staging into its KV pool locally.
  No RDMA, no IPC-handle churn: one fd import per (consumer, producer) pair,
  then direct device-to-device copies.

Scope: this is the v1 connector body wiring the VMM primitive (validated by
``tests/test_native_vmm_transfer.py``) into the KVConnector interface. Requests
in a step are transferred sequentially; a concurrent staging pool and the
DeepSeek-V4 slot/index regions are follow-ups. End-to-end 4P4D serving
validation is tracked in ROCm/ATOM#1483.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Any

import msgpack

from atom.config import Config
from atom.kv_transfer.disaggregation.base import (
    KVConnectorBase,
    KVConnectorSchedulerBase,
)
from atom.kv_transfer.disaggregation.native import vmm
from atom.kv_transfer.disaggregation.types import ConnectorMetadata, ReqMeta

logger = logging.getLogger("atom")

ReqId = str
TransferId = int

_MSG_WRITE_REQUEST = b"\x01"
_MSG_WRITE_DONE = b"\x02"


def _port_offset(dp_rank: int, tp_rank: int, tp_size: int = 1) -> int:
    return dp_rank * tp_size + tp_rank


def _sock_path(role: str, port: int) -> str:
    return f"/tmp/atom_native_{role}_{port}.sock"


# ---------------------------------------------------------------------------
# Scheduler (transport-agnostic): maps transfer_id <-> request_id and emits
# ConnectorMetadata. No third-party dependency.
# ---------------------------------------------------------------------------
class NativeConnectorScheduler(KVConnectorSchedulerBase):
    def __init__(self, config: Config) -> None:
        self.config = config
        kv_cfg = config.kv_transfer_config or {}
        self.is_producer = kv_cfg.get("kv_role", "kv_producer") == "kv_producer"
        self._reqs_to_save: dict[ReqId, ReqMeta] = {}
        self._reqs_to_recv: dict[ReqId, ReqMeta] = {}
        self.request_id_to_transfer_id: dict[ReqId, TransferId] = {}
        self.transfer_id_to_request_id: dict[TransferId, ReqId] = {}

    def get_num_new_matched_tokens(self, seq: Any) -> tuple[int, bool]:
        params = seq.kv_transfer_params or {}
        if params.get("do_remote_prefill") and not self.is_producer:
            return 0, True
        return 0, False

    def update_state_after_alloc(self, seq: Any) -> None:
        params = seq.kv_transfer_params or {}
        tid = params.get("transfer_id")
        if tid is not None:
            self.transfer_id_to_request_id[tid] = seq.id
            self.request_id_to_transfer_id[seq.id] = tid
        slot_idx = getattr(seq, "slot_index", -1)
        params["local_slot_index"] = slot_idx
        meta = ReqMeta(
            local_block_ids=list(getattr(seq, "block_ids", []) or []),
            remote_block_ids=params.get("remote_block_ids") or [],
            remote_host=params.get("remote_host", ""),
            remote_port=params.get("remote_port", 0),
            remote_handshake_port=params.get("remote_handshake_port", 0),
            remote_engine_id=params.get("remote_engine_id", ""),
            tp_size=params.get("tp_size", 1),
            remote_dp_size=params.get("remote_dp_size", 1),
            remote_dp_rank=params.get("remote_dp_rank", 0),
            transfer_id=params.get("transfer_id", 0),
            local_slot_index=slot_idx,
        )
        if params.get("do_remote_prefill"):
            assert not self.is_producer
            params["do_remote_prefill"] = False
            self._reqs_to_recv[seq.id] = meta
        elif params.get("do_remote_decode"):
            assert self.is_producer
            self._reqs_to_save[seq.id] = meta

    def build_connector_meta(self) -> ConnectorMetadata:
        meta = ConnectorMetadata()
        meta.request_id_to_transfer_id = dict(self.request_id_to_transfer_id)
        meta.reqs_to_save = dict(self._reqs_to_save)
        meta.reqs_to_recv = dict(self._reqs_to_recv)
        self._reqs_to_save.clear()
        self._reqs_to_recv.clear()
        return meta

    def request_finished(self, seq: Any) -> None:
        if self.is_producer:
            seq.kv_transfer_params_output = {
                "do_remote_prefill": True,
                "do_remote_decode": False,
                "transfer_id": seq.id,
            }
        tid = self.request_id_to_transfer_id.pop(seq.id, None)
        if tid is not None:
            self.transfer_id_to_request_id.pop(tid, None)


# ---------------------------------------------------------------------------
# Worker (transport): VMM staging + direct peer copy.
# ---------------------------------------------------------------------------
class NativeConnector(KVConnectorBase):
    def __init__(self, config: Config) -> None:
        self.config = config
        kv_cfg = config.kv_transfer_config or {}
        self.is_producer = kv_cfg.get("kv_role", "kv_producer") == "kv_producer"
        self.device = getattr(config, "device_id", 0)
        if not vmm.supported(self.device):
            raise RuntimeError(
                "kv_connector='native' requires HIP Virtual Memory Management "
                "(single-node scale-up); use 'moriio' for cross-node RDMA."
            )
        self.tp_rank = getattr(config, "tp_rank", 0)
        self.tp_size = getattr(config, "tp_size", 1)
        self.base_handshake_port = kv_cfg.get("handshake_port", 6501)
        self._port = self.base_handshake_port + _port_offset(
            0, self.tp_rank, self.tp_size
        )

        self._regions: list[tuple[int, int]] = []  # (base_addr, unit_bytes)
        self._staging: vmm.VmmBuffer | None = None
        self._staging_bytes = 0
        self._imported: dict[int, vmm.VmmBuffer] = {}  # consumer fd -> imported staging

        self._lock = threading.Lock()
        self.done_sending: set[ReqId] = set()
        self.done_recving: set[ReqId] = set()
        # producer: transfer_id -> src block ids (from reqs_to_save)
        self._src_blocks: dict[TransferId, list[int]] = {}

    # -- KVConnectorBase ----------------------------------------------------

    def register_kv_caches(self, kv_caches, transfer_tensors=None) -> None:
        if transfer_tensors is None:
            raise RuntimeError("native connector requires KV transfer tensors")
        for r in list(transfer_tensors.block_regions) + list(
            transfer_tensors.slot_regions
        ):
            self._regions.append((r.base_addr, r.unit_bytes))
        # staging holds the largest single request: one block per region is a
        # safe unit; grown lazily if a request needs more.
        self._staging_bytes = sum(ub for _, ub in self._regions) or (1 << 20)
        self._staging = vmm.VmmBuffer.alloc(self._staging_bytes, self.device)
        if self.is_producer:
            threading.Thread(target=self._serve, daemon=True).start()
        logger.info(
            "[native] registered %d KV regions, staging=%.1fMB (role=%s dev=%d)",
            len(self._regions),
            self._staging_bytes / (1 << 20),
            "producer" if self.is_producer else "consumer",
            self.device,
        )

    def start_load_kv(self, metadata: ConnectorMetadata) -> None:
        # producer: remember src block ids so the side channel can gather them.
        for req_id, meta in metadata.reqs_to_save.items():
            self._src_blocks[meta.transfer_id] = meta.local_block_ids
        # consumer: pull each pending request from its producer.
        for req_id, meta in metadata.reqs_to_recv.items():
            self._recv_request(req_id, meta)

    def get_finished(self) -> tuple[set, set]:
        with self._lock:
            ds, dr = set(self.done_sending), set(self.done_recving)
            self.done_sending.clear()
            self.done_recving.clear()
        return ds, dr

    def get_finished_recv_blocks(self) -> list[int]:
        return []

    # -- consumer side ------------------------------------------------------

    def _recv_request(self, req_id: ReqId, meta: ReqMeta) -> None:
        import torch

        self._grow_staging(len(meta.local_block_ids))
        payload = msgpack.dumps(
            {
                "req_id": req_id,
                "transfer_id": meta.transfer_id,
                "dst_block_ids": meta.local_block_ids,
            }
        )
        target = _sock_path(
            "p",
            meta.remote_handshake_port
            + _port_offset(meta.remote_dp_rank, self.tp_rank, meta.tp_size),
        )
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(target)
        socket.send_fds(s, [_MSG_WRITE_REQUEST + payload], [self._staging.export_fd()])
        resp = s.recv(4096)
        s.close()
        if resp[:1] != _MSG_WRITE_DONE:
            logger.error("[native] no WRITE_DONE for req %s", req_id)
            return
        # scatter staging -> local KV
        off = 0
        for base, unit in self._regions:
            for db in meta.local_block_ids:
                vmm.copy(base + db * unit, self._staging.data_ptr + off, unit)
                off += unit
        torch.cuda.synchronize(self.device)
        with self._lock:
            self.done_recving.add(req_id)

    # -- producer side ------------------------------------------------------

    def _serve(self) -> None:
        path = _sock_path("p", self._port)
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(64)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        import torch

        try:
            msg, fds, _, _ = socket.recv_fds(conn, 1 << 16, 1)
            if not msg or msg[:1] != _MSG_WRITE_REQUEST:
                return
            req = msgpack.loads(msg[1:])
            dst = self._imported.get(fds[0])
            if dst is None:
                dst = vmm.VmmBuffer.import_fd(fds[0], self._staging_bytes, self.device)
                self._imported[fds[0]] = dst
            src_blocks = self._src_blocks.get(req["transfer_id"], req["dst_block_ids"])
            # gather producer KV blocks straight into the consumer's staging
            off = 0
            for base, unit in self._regions:
                for sb in src_blocks:
                    vmm.copy(dst.data_ptr + off, base + sb * unit, unit)
                    off += unit
            torch.cuda.synchronize(self.device)
            conn.sendall(_MSG_WRITE_DONE + msgpack.dumps({"req_id": req["req_id"]}))
            with self._lock:
                self.done_sending.add(req["req_id"])
        except Exception:
            logger.exception("[native] producer handler error")
        finally:
            conn.close()

    def _grow_staging(self, nblocks: int) -> None:
        need = sum(ub for _, ub in self._regions) * max(1, nblocks)
        if need > self._staging_bytes:
            self._staging_bytes = need
            self._staging = vmm.VmmBuffer.alloc(need, self.device)
