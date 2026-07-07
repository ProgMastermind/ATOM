// SPDX-License-Identifier: MIT
// Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.
//
// Minimal HIP VMM bridge for cross-process single-node (XGMI) KV sharing:
// allocate an exportable VMM buffer, export/import its POSIX fd, grant peer
// access, and copy. Reliable cross-process where legacy hipIpc is not, and does
// not require the source GPU to be in the consumer's visible set.
#include <torch/extension.h>
#include <hip/hip_runtime.h>

#include <stdexcept>
#include <string>
#include <unordered_map>

#define HIPCK(expr)                                                          \
  do {                                                                       \
    hipError_t _e = (expr);                                                  \
    if (_e != hipSuccess)                                                    \
      throw std::runtime_error(std::string(#expr) + ": " +                  \
                               hipGetErrorString(_e));                       \
  } while (0)

namespace {

struct Region {
  hipMemGenericAllocationHandle_t handle;
  void *ptr;
  size_t size;
};

std::unordered_map<int64_t, Region> g_regions;
int64_t g_next_id = 0;

hipMemAllocationProp make_prop(int device) {
  hipMemAllocationProp prop{};
  prop.type = hipMemAllocationTypePinned;
  prop.location.type = hipMemLocationTypeDevice;
  prop.location.id = device;
  prop.requestedHandleType = hipMemHandleTypePosixFileDescriptor;
  return prop;
}

size_t round_up_to_granularity(size_t nbytes, int device) {
  hipMemAllocationProp prop = make_prop(device);
  size_t gran = 0;
  HIPCK(hipMemGetAllocationGranularity(&gran, &prop,
                                       hipMemAllocationGranularityMinimum));
  return ((nbytes + gran - 1) / gran) * gran;
}

// Grant `device` read/write peer access to a mapped range.
void grant_access(void *ptr, size_t size, int device) {
  hipMemAccessDesc desc{};
  desc.location.type = hipMemLocationTypeDevice;
  desc.location.id = device;
  desc.flags = hipMemAccessFlagsProtReadWrite;
  HIPCK(hipMemSetAccess(ptr, size, &desc, 1));
}

Region &region(int64_t id) {
  auto it = g_regions.find(id);
  if (it == g_regions.end())
    throw std::runtime_error("unknown VMM region id " + std::to_string(id));
  return it->second;
}

} // namespace

bool vmm_supported(int device) {
  int value = 0;
  hipDeviceGetAttribute(
      &value, hipDeviceAttributeVirtualMemoryManagementSupported, device);
  return value != 0;
}

// Allocate an exportable VMM buffer on `device`, map it and grant `device`
// access. Returns an opaque region id.
int64_t vmm_alloc(int64_t nbytes, int device) {
  HIPCK(hipSetDevice(device));
  size_t size = round_up_to_granularity(static_cast<size_t>(nbytes), device);
  hipMemAllocationProp prop = make_prop(device);
  Region r{};
  r.size = size;
  HIPCK(hipMemCreate(&r.handle, size, &prop, 0));
  HIPCK(hipMemAddressReserve(&r.ptr, size, 0, 0, 0));
  HIPCK(hipMemMap(r.ptr, size, 0, r.handle, 0));
  grant_access(r.ptr, size, device);
  int64_t id = g_next_id++;
  g_regions[id] = r;
  return id;
}

// Export the region's handle as a POSIX file descriptor (to send over a UNIX
// socket via SCM_RIGHTS).
int vmm_export_fd(int64_t id) {
  int fd = -1;
  HIPCK(hipMemExportToShareableHandle(
      &fd, region(id).handle, hipMemHandleTypePosixFileDescriptor, 0));
  return fd;
}

// Import a peer's fd, map it on `device` and grant `device` peer access.
// `nbytes` must match the producer's requested size (rounded identically).
int64_t vmm_import(int fd, int64_t nbytes, int device) {
  HIPCK(hipSetDevice(device));
  size_t size = round_up_to_granularity(static_cast<size_t>(nbytes), device);
  Region r{};
  r.size = size;
  HIPCK(hipMemImportFromShareableHandle(
      &r.handle, reinterpret_cast<void *>(static_cast<intptr_t>(fd)),
      hipMemHandleTypePosixFileDescriptor));
  HIPCK(hipMemAddressReserve(&r.ptr, size, 0, 0, 0));
  HIPCK(hipMemMap(r.ptr, size, 0, r.handle, 0));
  grant_access(r.ptr, size, device);
  int64_t id = g_next_id++;
  g_regions[id] = r;
  return id;
}

// Wrap the first `nbytes` of the mapped region as a (non-owning) uint8 tensor.
int64_t vmm_ptr(int64_t id) {
  return reinterpret_cast<int64_t>(region(id).ptr);
}

torch::Tensor vmm_tensor(int64_t id, int64_t nbytes, int device) {
  auto opts = torch::TensorOptions().dtype(torch::kUInt8).device(
      torch::kCUDA, device);
  return torch::from_blob(region(id).ptr, {nbytes}, opts);
}

// Device-to-device copy between two raw device pointers (peer-mapped ok). Used
// by the connector to gather/scatter KV blocks to/from the VMM staging region.
void vmm_copy(int64_t dst_ptr, int64_t src_ptr, int64_t nbytes) {
  HIPCK(hipMemcpy(reinterpret_cast<void *>(dst_ptr),
                  reinterpret_cast<void *>(src_ptr),
                  static_cast<size_t>(nbytes), hipMemcpyDeviceToDevice));
}

void vmm_free(int64_t id) {
  auto it = g_regions.find(id);
  if (it == g_regions.end())
    return;
  Region &r = it->second;
  hipMemUnmap(r.ptr, r.size);
  hipMemAddressFree(r.ptr, r.size);
  hipMemRelease(r.handle);
  g_regions.erase(it);
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  m.def("vmm_supported", &vmm_supported);
  m.def("vmm_alloc", &vmm_alloc);
  m.def("vmm_export_fd", &vmm_export_fd);
  m.def("vmm_import", &vmm_import);
  m.def("vmm_ptr", &vmm_ptr);
  m.def("vmm_tensor", &vmm_tensor);
  m.def("vmm_copy", &vmm_copy);
  m.def("vmm_free", &vmm_free);
}
