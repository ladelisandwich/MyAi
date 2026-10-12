"""Hardware report and live telemetry schemas exchanged between workers and the coordinator.

Every value that could not be measured is ``None`` and, where useful, explained in
``unavailable`` - HomeMesh never substitutes guessed numbers for missing telemetry.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

GpuVendor = Literal["nvidia", "amd", "intel", "apple", "other"]


class OSInfo(BaseModel):
    system: str  # Linux | Windows | Darwin
    release: str = ""
    version: str = ""
    distro_id: str | None = None  # e.g. "bazzite", "fedora", "ubuntu"
    distro_name: str | None = None
    immutable: bool = False  # rpm-ostree/ostree based (Bazzite, Silverblue, ...)
    in_container: bool = False
    hostname: str = ""
    arch: str = ""
    python: str = ""


class CPUInfo(BaseModel):
    model: str | None = None
    vendor: str | None = None
    physical_cores: int | None = None
    logical_cores: int | None = None
    max_freq_mhz: float | None = None
    flags: list[str] = Field(default_factory=list)  # selected ISA flags: avx2, avx512f, ...


class MemoryInfo(BaseModel):
    total_mb: int | None = None
    available_mb: int | None = None
    swap_total_mb: int | None = None


class GPUInfo(BaseModel):
    index: int
    vendor: GpuVendor
    name: str
    uuid: str | None = None
    driver_version: str | None = None
    vram_total_mb: int | None = None
    vram_used_mb: int | None = None
    vram_free_mb: int | None = None
    utilization_percent: float | None = None
    temperature_c: float | None = None
    integrated: bool | None = None
    compute_apis: list[str] = Field(default_factory=list)  # cuda, rocm, vulkan, metal, directml...
    compute_capability: str | None = None
    pci_bus_id: str | None = None
    # Excluded by the worker's ``ignore_gpus`` setting: shown, but not counted for scheduling.
    ignored: bool = False
    source: str = ""  # nvidia-smi | sysfs | wmi | system_profiler
    unavailable: dict[str, str] = Field(default_factory=dict)  # field -> reason it is not available


class DiskInfo(BaseModel):
    path: str
    total_mb: int | None = None
    free_mb: int | None = None


class HardwareReport(BaseModel):
    os: OSInfo
    cpu: CPUInfo
    memory: MemoryInfo
    gpus: list[GPUInfo] = Field(default_factory=list)
    disks: list[DiskInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    collected_at: str = ""

    @property
    def total_vram_mb(self) -> int | None:
        known = [g.vram_total_mb for g in self.gpus if g.vram_total_mb is not None and not g.integrated]
        return sum(known) if known else None


class GPUTelemetry(BaseModel):
    index: int
    utilization_percent: float | None = None
    vram_used_mb: int | None = None
    vram_free_mb: int | None = None
    vram_total_mb: int | None = None
    temperature_c: float | None = None


class Telemetry(BaseModel):
    """Live utilisation sampled by the worker and sent with every heartbeat."""

    cpu_percent: float | None = None
    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    load_avg_1m: float | None = None
    gpus: list[GPUTelemetry] = Field(default_factory=list)
    # True when the GPU is busy while this worker runs no jobs, i.e. something else (a game,
    # a render) is using it. Resource profiles use this to back off cooperatively.
    external_gpu_busy: bool = False
    sampled_at: str = ""
