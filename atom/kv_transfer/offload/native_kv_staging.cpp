// SPDX-License-Identifier: MIT
// Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

#include <ATen/hip/HIPContext.h>
#include <ATen/hip/impl/HIPGuardImplMasqueradingAsCUDA.h>
#include <hip/hip_runtime.h>
#include <torch/extension.h>

#include <cstdint>
#include <vector>

#define ATOM_HIP_CHECK(cmd)                                                \
  do {                                                                     \
    hipError_t err = (cmd);                                                \
    TORCH_CHECK(                                                           \
        err == hipSuccess, "HIP error: ", hipGetErrorString(err));         \
  } while (0)

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
    hipStream_t stream);

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
    hipStream_t stream);

namespace {

torch::Tensor make_device_i64(
    const std::vector<int64_t>& values,
    const torch::Device& device,
    hipStream_t stream) {
  auto tensor = torch::empty(
      {static_cast<int64_t>(values.size())},
      torch::TensorOptions().dtype(torch::kInt64).device(device));
  if (!values.empty()) {
    ATOM_HIP_CHECK(hipMemcpyAsync(
        tensor.data_ptr<int64_t>(),
        values.data(),
        values.size() * sizeof(int64_t),
        hipMemcpyHostToDevice,
        stream));
  }
  return tensor;
}

struct StagingMeta {
  torch::Tensor segment_ptrs;
  torch::Tensor segment_block_bytes;
  torch::Tensor segment_prefix_bytes;
  torch::Tensor chunk_block_counts;
  torch::Tensor chunk_block_offsets;
  torch::Tensor chunk_output_bases;
  torch::Tensor block_ids;
  int64_t num_chunks = 0;
  int64_t num_segments = 0;
  int64_t total_bytes = 0;
};

StagingMeta build_meta(
    const std::vector<torch::Tensor>& segment_tensors,
    const std::vector<int64_t>& segment_block_bytes,
    const std::vector<int64_t>& chunk_block_counts,
    const std::vector<int64_t>& block_ids,
    torch::Tensor device_buf,
    hipStream_t stream) {
  TORCH_CHECK(device_buf.is_cuda(), "device_buf must be a CUDA/HIP tensor");
  TORCH_CHECK(device_buf.dtype() == torch::kUInt8, "device_buf must be uint8");
  TORCH_CHECK(device_buf.is_contiguous(), "device_buf must be contiguous");
  TORCH_CHECK(
      segment_tensors.size() == segment_block_bytes.size(),
      "segment_tensors and segment_block_bytes size mismatch");

  const int64_t num_segments = static_cast<int64_t>(segment_tensors.size());
  const int64_t num_chunks = static_cast<int64_t>(chunk_block_counts.size());
  TORCH_CHECK(num_segments > 0, "at least one segment is required");

  std::vector<int64_t> segment_ptr_values(num_segments);
  std::vector<int64_t> segment_prefix_values(num_segments);
  int64_t bytes_per_block = 0;
  for (int64_t i = 0; i < num_segments; ++i) {
    const auto& seg = segment_tensors[i];
    TORCH_CHECK(seg.is_cuda(), "segment tensor must be CUDA/HIP");
    TORCH_CHECK(seg.device() == device_buf.device(), "segment/device mismatch");
    TORCH_CHECK(seg.is_contiguous(), "segment tensor must be contiguous");
    TORCH_CHECK(segment_block_bytes[i] > 0, "segment block bytes must be > 0");
    segment_ptr_values[i] =
        reinterpret_cast<int64_t>(static_cast<uint8_t*>(seg.data_ptr()));
    segment_prefix_values[i] = bytes_per_block;
    bytes_per_block += segment_block_bytes[i];
  }

  std::vector<int64_t> chunk_block_offsets(num_chunks);
  std::vector<int64_t> chunk_output_bases(num_chunks);
  int64_t block_offset = 0;
  int64_t byte_offset = 0;
  for (int64_t c = 0; c < num_chunks; ++c) {
    const int64_t nblocks = chunk_block_counts[c];
    TORCH_CHECK(nblocks >= 0, "chunk block count must be non-negative");
    chunk_block_offsets[c] = block_offset;
    chunk_output_bases[c] = byte_offset;
    block_offset += nblocks;
    byte_offset += nblocks * bytes_per_block;
  }
  TORCH_CHECK(
      static_cast<int64_t>(block_ids.size()) == block_offset,
      "block_ids length does not match chunk block counts");
  TORCH_CHECK(
      device_buf.numel() >= byte_offset,
      "device_buf is smaller than chunk-major staging output");

  StagingMeta meta;
  meta.segment_ptrs = make_device_i64(segment_ptr_values, device_buf.device(), stream);
  meta.segment_block_bytes =
      make_device_i64(segment_block_bytes, device_buf.device(), stream);
  meta.segment_prefix_bytes =
      make_device_i64(segment_prefix_values, device_buf.device(), stream);
  meta.chunk_block_counts =
      make_device_i64(chunk_block_counts, device_buf.device(), stream);
  meta.chunk_block_offsets =
      make_device_i64(chunk_block_offsets, device_buf.device(), stream);
  meta.chunk_output_bases =
      make_device_i64(chunk_output_bases, device_buf.device(), stream);
  meta.block_ids = make_device_i64(block_ids, device_buf.device(), stream);
  meta.num_chunks = num_chunks;
  meta.num_segments = num_segments;
  meta.total_bytes = byte_offset;
  return meta;
}

hipStream_t current_hip_stream(torch::Device device) {
  c10::hip::HIPGuardMasqueradingAsCUDA guard(device);
  auto stream = at::hip::getCurrentHIPStreamMasqueradingAsCUDA(device.index());
  return stream.stream();
}

}  // namespace

void fused_pack_chunk_major(
    std::vector<torch::Tensor> segment_tensors,
    std::vector<int64_t> segment_block_bytes,
    std::vector<int64_t> chunk_block_counts,
    std::vector<int64_t> block_ids,
    torch::Tensor device_buf) {
  auto stream = current_hip_stream(device_buf.device());
  auto meta = build_meta(
      segment_tensors,
      segment_block_bytes,
      chunk_block_counts,
      block_ids,
      device_buf,
      stream);
  if (meta.total_bytes == 0) {
    return;
  }
  ATOM_HIP_CHECK(launch_fused_pack_chunk_major(
      device_buf.data_ptr<uint8_t>(),
      meta.segment_ptrs.data_ptr<int64_t>(),
      meta.segment_block_bytes.data_ptr<int64_t>(),
      meta.segment_prefix_bytes.data_ptr<int64_t>(),
      meta.chunk_block_counts.data_ptr<int64_t>(),
      meta.chunk_block_offsets.data_ptr<int64_t>(),
      meta.chunk_output_bases.data_ptr<int64_t>(),
      meta.block_ids.data_ptr<int64_t>(),
      meta.num_chunks,
      meta.num_segments,
      stream));
}

void fused_unpack_chunk_major(
    torch::Tensor device_buf,
    std::vector<torch::Tensor> segment_tensors,
    std::vector<int64_t> segment_block_bytes,
    std::vector<int64_t> chunk_block_counts,
    std::vector<int64_t> block_ids) {
  auto stream = current_hip_stream(device_buf.device());
  auto meta = build_meta(
      segment_tensors,
      segment_block_bytes,
      chunk_block_counts,
      block_ids,
      device_buf,
      stream);
  if (meta.total_bytes == 0) {
    return;
  }
  ATOM_HIP_CHECK(launch_fused_unpack_chunk_major(
      device_buf.data_ptr<uint8_t>(),
      meta.segment_ptrs.data_ptr<int64_t>(),
      meta.segment_block_bytes.data_ptr<int64_t>(),
      meta.segment_prefix_bytes.data_ptr<int64_t>(),
      meta.chunk_block_counts.data_ptr<int64_t>(),
      meta.chunk_block_offsets.data_ptr<int64_t>(),
      meta.chunk_output_bases.data_ptr<int64_t>(),
      meta.block_ids.data_ptr<int64_t>(),
      meta.num_chunks,
      meta.num_segments,
      stream));
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  m.def("fused_pack_chunk_major", &fused_pack_chunk_major);
  m.def("fused_unpack_chunk_major", &fused_unpack_chunk_major);
}
