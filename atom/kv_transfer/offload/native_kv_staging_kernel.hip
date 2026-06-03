// SPDX-License-Identifier: MIT
// Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

#include <hip/hip_runtime.h>

#include <cstdint>

namespace {

constexpr int kThreads = 256;

__global__ void pack_chunk_major_kernel(
    uint8_t* __restrict__ device_buf,
    const int64_t* __restrict__ segment_ptrs,
    const int64_t* __restrict__ segment_block_bytes,
    const int64_t* __restrict__ segment_prefix_bytes,
    const int64_t* __restrict__ chunk_block_counts,
    const int64_t* __restrict__ chunk_block_offsets,
    const int64_t* __restrict__ chunk_output_bases,
    const int64_t* __restrict__ block_ids,
    int64_t num_segments) {
  const int64_t job = static_cast<int64_t>(blockIdx.x);
  const int64_t chunk_id = job / num_segments;
  const int64_t seg_id = job - chunk_id * num_segments;
  const int64_t nblocks = chunk_block_counts[chunk_id];
  const int64_t seg_bytes = segment_block_bytes[seg_id];
  const int64_t nbytes = nblocks * seg_bytes;
  if (nbytes <= 0) {
    return;
  }

  const auto* src_base =
      reinterpret_cast<const uint8_t*>(segment_ptrs[seg_id]);
  uint8_t* dst_base = device_buf + chunk_output_bases[chunk_id] +
      segment_prefix_bytes[seg_id] * nblocks;
  const int64_t block_offset = chunk_block_offsets[chunk_id];

  for (int64_t i = static_cast<int64_t>(threadIdx.x); i < nbytes;
       i += static_cast<int64_t>(blockDim.x)) {
    const int64_t local_block = i / seg_bytes;
    const int64_t byte_in_block = i - local_block * seg_bytes;
    const int64_t physical_block = block_ids[block_offset + local_block];
    dst_base[i] = src_base[physical_block * seg_bytes + byte_in_block];
  }
}

__global__ void unpack_chunk_major_kernel(
    const uint8_t* __restrict__ device_buf,
    const int64_t* __restrict__ segment_ptrs,
    const int64_t* __restrict__ segment_block_bytes,
    const int64_t* __restrict__ segment_prefix_bytes,
    const int64_t* __restrict__ chunk_block_counts,
    const int64_t* __restrict__ chunk_block_offsets,
    const int64_t* __restrict__ chunk_output_bases,
    const int64_t* __restrict__ block_ids,
    int64_t num_segments) {
  const int64_t job = static_cast<int64_t>(blockIdx.x);
  const int64_t chunk_id = job / num_segments;
  const int64_t seg_id = job - chunk_id * num_segments;
  const int64_t nblocks = chunk_block_counts[chunk_id];
  const int64_t seg_bytes = segment_block_bytes[seg_id];
  const int64_t nbytes = nblocks * seg_bytes;
  if (nbytes <= 0) {
    return;
  }

  const auto* src_base = device_buf + chunk_output_bases[chunk_id] +
      segment_prefix_bytes[seg_id] * nblocks;
  auto* dst_base = reinterpret_cast<uint8_t*>(segment_ptrs[seg_id]);
  const int64_t block_offset = chunk_block_offsets[chunk_id];

  for (int64_t i = static_cast<int64_t>(threadIdx.x); i < nbytes;
       i += static_cast<int64_t>(blockDim.x)) {
    const int64_t local_block = i / seg_bytes;
    const int64_t byte_in_block = i - local_block * seg_bytes;
    const int64_t physical_block = block_ids[block_offset + local_block];
    dst_base[physical_block * seg_bytes + byte_in_block] = src_base[i];
  }
}

}  // namespace

hipError_t launch_fused_pack_chunk_major(
    uint8_t* device_buf,
    const int64_t* segment_ptrs,
    const int64_t* segment_block_bytes,
    const int64_t* segment_prefix_bytes,
    const int64_t* chunk_block_counts,
    const int64_t* chunk_block_offsets,
    const int64_t* chunk_output_bases,
    const int64_t* block_ids,
    int64_t num_chunks,
    int64_t num_segments,
    hipStream_t stream) {
  const dim3 grid(static_cast<unsigned int>(num_chunks * num_segments));
  const dim3 block(kThreads);
  hipLaunchKernelGGL(
      pack_chunk_major_kernel,
      grid,
      block,
      0,
      stream,
      device_buf,
      segment_ptrs,
      segment_block_bytes,
      segment_prefix_bytes,
      chunk_block_counts,
      chunk_block_offsets,
      chunk_output_bases,
      block_ids,
      num_segments);
  return hipGetLastError();
}

hipError_t launch_fused_unpack_chunk_major(
    const uint8_t* device_buf,
    const int64_t* segment_ptrs,
    const int64_t* segment_block_bytes,
    const int64_t* segment_prefix_bytes,
    const int64_t* chunk_block_counts,
    const int64_t* chunk_block_offsets,
    const int64_t* chunk_output_bases,
    const int64_t* block_ids,
    int64_t num_chunks,
    int64_t num_segments,
    hipStream_t stream) {
  const dim3 grid(static_cast<unsigned int>(num_chunks * num_segments));
  const dim3 block(kThreads);
  hipLaunchKernelGGL(
      unpack_chunk_major_kernel,
      grid,
      block,
      0,
      stream,
      device_buf,
      segment_ptrs,
      segment_block_bytes,
      segment_prefix_bytes,
      chunk_block_counts,
      chunk_block_offsets,
      chunk_output_bases,
      block_ids,
      num_segments);
  return hipGetLastError();
}
