# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""Native single-node KV connector (HIP VMM, no third-party transport).

In-tree prefill/decode KV connector for the single-node (scale-up / XGMI) case.
Depends only on the HIP Virtual Memory Management API
(:mod:`atom.kv_transfer.disaggregation.native.vmm`) — no MoRI, no Mooncake.

Selected with ``--kv-transfer-config '{"kv_connector":"native", ...}'``.

Data path (push, producer -> consumer), all staged through the consumer's VMM
buffer so no model-side allocation change is required:
  * The consumer allocates a VMM staging buffer per pending request, exports its
    POSIX fd, and sends it (with the request's dst block ids / slot) to the
    producer over a UNIX side channel.
  * The producer imports the staging (granting its own device access), gathers
    the request's KV blocks + SWA slots + compressor state into it over XGMI
    (``hipMemcpy`` peer), and replies WRITE_DONE.
  * The consumer scatters from its staging into its KV pool / state.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Any

import msgpack
import torch
import zmq

from aiter.dist.parallel_state import get_dp_group, get_tp_group
from atom.config import Config
from atom.kv_transfer.disaggregation.base import (
    KVConnectorBase,
    KVConnectorSchedulerBase,
)
from atom.kv_transfer.disaggregation.native import vmm
from atom.kv_transfer.disaggregation.types import ConnectorMetadata, ReqMeta
from atom.utils.network import get_ip

logger = logging.getLogger("atom")

ReqId = str
TransferId = int

_MSG_WRITE_REQUEST = b"\x01"
_MSG_WRITE_DONE = b"\x02"
_PREFILL_WAIT_S = 30.0


def _port_offset(dp_rank: int, tp_rank: int, tp_size: int = 1) -> int:
    return dp_rank * tp_size + tp_rank


def _sock_path(port: int) -> str:
    return f"/tmp/atom_native_p_{port}.sock"


# ---------------------------------------------------------------------------
# Scheduler (transport-agnostic).
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
        slot_idx = getattr(seq, "per_req_cache_group", getattr(seq, "slot_index", -1))
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
            # The transfer handle the consumer will request is the producer's
            # own request id (see request_finished), not params["transfer_id"].
            meta.transfer_id = seq.id
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
# Worker (VMM transport).
# ---------------------------------------------------------------------------
class NativeConnector(KVConnectorBase):
    def __init__(self, config: Config) -> None:
        self.config = config
        kv_cfg = config.kv_transfer_config or {}
        self.is_producer = kv_cfg.get("kv_role", "kv_producer") == "kv_producer"
        self.device = torch.cuda.current_device()
        if not vmm.supported(self.device):
            raise RuntimeError(
                "kv_connector='native' requires HIP VMM (single-node scale-up); "
                "use 'moriio' for cross-node RDMA."
            )
        self.tp_rank = get_tp_group().rank_in_group
        self.tp_size = get_tp_group().world_size
        self.dp_rank = get_dp_group().rank_in_group
        self.dp_size = get_dp_group().world_size
        self.local_ip = get_ip()
        self.http_port = kv_cfg.get("http_port", 8000)
        self.request_address = f"{self.local_ip}:{self.http_port}"
        self.proxy_ip = kv_cfg.get("proxy_ip")
        self.proxy_ping_port = kv_cfg.get("proxy_ping_port", 36367)
        self.base_handshake_port = kv_cfg.get("handshake_port", 6501)
        self._port = self.base_handshake_port + _port_offset(
            self.dp_rank, self.tp_rank, self.tp_size
        )

        # KV region layout (filled by register_kv_caches).
        self._block_regions: list[tuple[int, int]] = []  # (base, bytes/block)
        self._slot_regions: list[tuple[int, int]] = []  # (base, bytes/slot)
        self._state_base = 0
        self._state_slot_bytes = 0
        self._state_pool_free: list[int] = []
        self._gather_slot = None
        self._scatter_slot = None

        self._lock = threading.Lock()
        self.done_sending: set[ReqId] = set()
        self.done_recving: set[ReqId] = set()
        # producer: transfer_id -> (src_block_ids, src_slot)
        self._prefills: dict[TransferId, tuple[list[int], int]] = {}
        self._prefills_cv = threading.Condition(self._lock)
        self._imported: dict[int, vmm.VmmBuffer] = {}

        self._zmq = zmq.Context()
        if self.tp_rank == 0 and self.dp_rank == 0 and self.proxy_ip:
            threading.Thread(target=self._ping, daemon=True).start()

    # -- proxy service discovery -------------------------------------------

    def _ping(self) -> None:
        path = f"tcp://{self.proxy_ip}:{self.proxy_ping_port}"
        role = "P" if self.is_producer else "D"
        with self._zmq.socket(zmq.DEALER) as sock:
            sock.connect(path)
            i = 1
            while True:
                try:
                    sock.send(
                        msgpack.dumps(
                            {
                                "type": "register",
                                "role": role,
                                "index": str(i),
                                "request_address": f"http://{self.request_address}/v1/completions",
                                "rpc_port": self._port,
                                "handshake_port": self.base_handshake_port,
                                "dp_size": self.dp_size,
                                "tp_size": self.tp_size,
                                "transfer_mode": "write",
                            }
                        )
                    )
                    i += 1
                except Exception:
                    pass
                time.sleep(5.0)

    # -- KVConnectorBase ----------------------------------------------------

    def register_kv_caches(self, kv_caches, transfer_tensors=None) -> None:
        tt = transfer_tensors
        if tt is None:
            raise RuntimeError("native connector requires KV transfer tensors")
        self._block_regions = [(r.base_addr, r.unit_bytes) for r in tt.block_regions]
        self._slot_regions = [(r.base_addr, r.unit_bytes) for r in tt.slot_regions]
        self._gather_slot = tt.gather_slot
        self._scatter_slot = tt.scatter_slot
        if tt.staging_region is not None:
            self._state_base = tt.staging_region.base_addr
            self._state_slot_bytes = tt.staging_region.unit_bytes
            self._state_pool_free = list(range(tt.staging_pool_size))
        logger.info(
            "[native] registered %d block + %d slot regions, state_slot=%dB "
            "(role=%s dev=%d rank=%d)",
            len(self._block_regions),
            len(self._slot_regions),
            self._state_slot_bytes,
            "producer" if self.is_producer else "consumer",
            self.device,
            self.tp_rank,
        )
        if self.is_producer:
            threading.Thread(target=self._serve, daemon=True).start()

    def start_load_kv(self, metadata: ConnectorMetadata) -> None:
        for _, meta in metadata.reqs_to_save.items():
            with self._prefills_cv:
                self._prefills[meta.transfer_id] = (
                    meta.local_block_ids,
                    meta.local_slot_index,
                )
                self._prefills_cv.notify_all()
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

    # -- staging layout -----------------------------------------------------

    def _req_bytes(self, nblocks: int) -> int:
        b = sum(bpb for _, bpb in self._block_regions) * nblocks
        b += sum(bps for _, bps in self._slot_regions)
        b += self._state_slot_bytes
        return b

    def _acquire_state_slot(self) -> int:
        with self._lock:
            return self._state_pool_free.pop() if self._state_pool_free else -1

    def _release_state_slot(self, idx: int) -> None:
        if idx >= 0:
            with self._lock:
                self._state_pool_free.append(idx)

    # -- consumer -----------------------------------------------------------

    def _recv_request(self, req_id: ReqId, meta: ReqMeta) -> None:
        nblocks = len(meta.local_block_ids)
        staging = vmm.VmmBuffer.alloc(self._req_bytes(nblocks), self.device)
        payload = msgpack.dumps(
            {
                "req_id": req_id,
                "transfer_id": meta.transfer_id,
                "dst_block_ids": meta.local_block_ids,
                "dst_slot": meta.local_slot_index,
            }
        )
        target = _sock_path(
            meta.remote_handshake_port
            + _port_offset(meta.remote_dp_rank, self.tp_rank, meta.tp_size)
        )
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(target)
        socket.send_fds(s, [_MSG_WRITE_REQUEST + payload], [staging.export_fd()])
        resp = s.recv(4096)
        s.close()
        if resp[:1] != _MSG_WRITE_DONE:
            logger.error("[native] no WRITE_DONE for req %s", req_id)
            return
        self._scatter(staging, meta.local_block_ids, meta.local_slot_index)
        with self._lock:
            self.done_recving.add(req_id)

    def _scatter(self, staging, dst_block_ids, dst_slot) -> None:
        off = 0
        for base, bpb in self._block_regions:
            for db in dst_block_ids:
                vmm.copy(base + db * bpb, staging.data_ptr + off, bpb)
                off += bpb
        for base, bps in self._slot_regions:
            if dst_slot >= 0:
                vmm.copy(base + dst_slot * bps, staging.data_ptr + off, bps)
            off += bps
        if self._state_slot_bytes and dst_slot >= 0 and self._scatter_slot is not None:
            pool_idx = self._acquire_state_slot()
            if pool_idx >= 0:
                vmm.copy(
                    self._state_base + pool_idx * self._state_slot_bytes,
                    staging.data_ptr + off,
                    self._state_slot_bytes,
                )
                self._scatter_slot(dst_slot, pool_idx)
                self._release_state_slot(pool_idx)
        torch.cuda.synchronize(self.device)

    # -- producer -----------------------------------------------------------

    def _serve(self) -> None:
        path = _sock_path(self._port)
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(64)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            msg, fds, _, _ = socket.recv_fds(conn, 1 << 16, 1)
            if not msg or msg[:1] != _MSG_WRITE_REQUEST:
                return
            req = msgpack.loads(msg[1:])
            dst = self._imported.get(fds[0])
            if dst is None:
                nblocks = len(req["dst_block_ids"])
                dst = vmm.VmmBuffer.import_fd(
                    fds[0], self._req_bytes(nblocks), self.device
                )
                self._imported[fds[0]] = dst
            self._gather(dst, req["transfer_id"])
            conn.sendall(_MSG_WRITE_DONE + msgpack.dumps({"req_id": req["req_id"]}))
            with self._lock:
                self.done_sending.add(req["req_id"])
        except Exception:
            logger.exception("[native] producer handler error")
        finally:
            conn.close()

    def _gather(self, staging, transfer_id: int) -> None:
        with self._prefills_cv:
            self._prefills_cv.wait_for(
                lambda: transfer_id in self._prefills, timeout=_PREFILL_WAIT_S
            )
            src_block_ids, src_slot = self._prefills.get(transfer_id, ([], -1))
        off = 0
        for base, bpb in self._block_regions:
            for sb in src_block_ids:
                vmm.copy(staging.data_ptr + off, base + sb * bpb, bpb)
                off += bpb
        for base, bps in self._slot_regions:
            if src_slot >= 0:
                vmm.copy(staging.data_ptr + off, base + src_slot * bps, bps)
            off += bps
        if self._state_slot_bytes and src_slot >= 0 and self._gather_slot is not None:
            pool_idx = self._acquire_state_slot()
            if pool_idx >= 0:
                self._gather_slot(src_slot, pool_idx)
                torch.cuda.current_stream().synchronize()
                vmm.copy(
                    staging.data_ptr + off,
                    self._state_base + pool_idx * self._state_slot_bytes,
                    self._state_slot_bytes,
                )
                self._release_state_slot(pool_idx)
        torch.cuda.synchronize(self.device)
