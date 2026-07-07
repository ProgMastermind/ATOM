# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""Cross-process test for HIP VMM based KV transfer.

A producer process allocates an exportable VMM buffer on GPU 0, fills it, and
passes the POSIX fd to a consumer process (GPU 1) over a UNIX socket. The
consumer imports it and peer-copies blocks directly over the fabric, then
verifies the data. Requires >= 2 GPUs with VMM support; skips otherwise.
"""

from __future__ import annotations

import socket

import pytest
import torch
import torch.multiprocessing as mp

NB, BE = 256, 4096  # 256 blocks x 4096 bf16 = 2 MiB; values < 128 are bf16-exact
NBYTES = NB * BE * 2
NCOPY = 64


def _producer(path, device, ready):
    from atom.kv_transfer.disaggregation.native import VmmBuffer

    torch.cuda.set_device(device)
    buf = VmmBuffer.alloc(NBYTES, device)
    kv = buf.tensor(torch.bfloat16, (NB, BE))
    for i in range(NB):
        kv[i].fill_(float(i % 128))
    torch.cuda.synchronize()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    ready.set()
    conn, _ = srv.accept()
    socket.send_fds(conn, [b"vmm"], [buf.export_fd()])
    conn.recv(1)  # keep the buffer alive until the consumer is done
    conn.close()
    srv.close()


def _consumer(path, device, ready, result):
    from atom.kv_transfer.disaggregation.native import VmmBuffer

    torch.cuda.set_device(device)
    ready.wait(60)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    _, fds, _, _ = socket.recv_fds(s, 16, 1)

    peer = VmmBuffer.import_fd(fds[0], NBYTES, device).tensor(torch.bfloat16, (NB, BE))
    local = torch.empty(NB, BE, dtype=torch.bfloat16, device=device)

    # concurrent per-"request" peer copies across streams (hipMemcpyPeerAsync)
    streams = [torch.cuda.Stream(device=device) for _ in range(NCOPY)]
    for i, st in enumerate(streams):
        with torch.cuda.stream(st):
            local[i % NB].copy_(peer[(i * 7) % NB], non_blocking=True)
    for st in streams:
        st.synchronize()

    ok = all(
        local[i % NB][0].item() == float(((i * 7) % NB) % 128) for i in range(NCOPY)
    )
    result.put(bool(ok))
    s.send(b"d")
    s.close()


@pytest.mark.skipif(
    not torch.cuda.is_available() or torch.cuda.device_count() < 2,
    reason="requires >= 2 GPUs",
)
def test_vmm_cross_process_transfer(tmp_path):
    from atom.kv_transfer.disaggregation.native import supported

    if not supported(0) or not supported(1):
        pytest.skip("HIP VMM not supported on these devices")

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    result = ctx.Queue()
    path = str(tmp_path / "vmm.sock")

    prod = ctx.Process(target=_producer, args=(path, 0, ready))
    cons = ctx.Process(target=_consumer, args=(path, 1, ready, result))
    prod.start()
    cons.start()
    cons.join(180)
    prod.join(30)

    assert cons.exitcode == 0, "consumer process crashed"
    assert prod.exitcode == 0, "producer process crashed"
    assert result.get(timeout=5) is True, "peer-copied data mismatch"
