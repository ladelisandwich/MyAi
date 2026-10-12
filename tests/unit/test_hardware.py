import json
from pathlib import Path

from homemesh.hardware.cpu_mem import parse_proc_cpuinfo
from homemesh.hardware.detect import _merge_gpus
from homemesh.hardware.gpu_linux_sysfs import detect_sysfs, sample_sysfs
from homemesh.hardware.gpu_macos import parse_system_profiler
from homemesh.hardware.gpu_nvidia import QUERY_FIELDS, parse_query_output, rows_to_gpus
from homemesh.hardware.gpu_windows import parse_windows_gpu_json
from homemesh.hardware.osinfo import parse_os_release

# Captured format of: nvidia-smi --query-gpu=<QUERY_FIELDS> --format=csv,noheader,nounits
RTX3080 = "0, NVIDIA GeForce RTX 3080, GPU-1c2f3a4b-aaaa-bbbb-cccc-1234567890ab, 560.35.03, 10240, 1187, 8820, 7, 41, 00000000:01:00.0, 8.6\n"


def test_nvidia_rtx3080_parsing_uses_reported_vram():
    gpus = rows_to_gpus(parse_query_output(RTX3080, QUERY_FIELDS))
    assert len(gpus) == 1
    g = gpus[0]
    assert g.name == "NVIDIA GeForce RTX 3080"
    assert g.vram_total_mb == 10240 and g.vram_free_mb == 8820 and g.vram_used_mb == 1187
    assert g.utilization_percent == 7 and g.temperature_c == 41
    assert g.compute_apis == ["cuda"] and g.compute_capability == "8.6" and g.integrated is False
    assert g.unavailable == {}


def test_nvidia_not_supported_fields_are_marked_unavailable():
    line = "0, NVIDIA RTX A2000 Laptop GPU, GPU-x, 550.1, 4096, [N/A], [N/A], [Not Supported], [N/A], 00000000:02:00.0, 8.6"
    g = rows_to_gpus(parse_query_output(line, QUERY_FIELDS))[0]
    assert g.vram_total_mb == 4096
    assert g.vram_used_mb is None and g.utilization_percent is None
    assert "utilization_percent" in g.unavailable and "vram_used_mb" in g.unavailable


def test_nvidia_multi_gpu_and_garbage_lines():
    text = RTX3080 + "garbage line without commas\n" + RTX3080.replace("0, NVIDIA", "1, NVIDIA")
    assert [g.index for g in rows_to_gpus(parse_query_output(text, QUERY_FIELDS))] == [0, 1]


def _fake_card(root: Path, n: int, vendor: str, **files: str) -> None:
    dev = root / f"card{n}" / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor)
    (dev / "device").write_text("0x73bf")
    (dev / "uevent").write_text(f"DRIVER=amdgpu\nPCI_SLOT_NAME=0000:0{n}:00.0\n")
    for k, v in files.items():
        (dev / k).write_text(v)
    (root / f"card{n}-HDMI-A-1").mkdir()  # connector entries must be ignored


def test_sysfs_amd_discrete_reports_real_vram(tmp_path):
    _fake_card(tmp_path, 0, "0x1002", mem_info_vram_total=str(16 * 2**30), mem_info_vram_used=str(2 * 2**30),
               gpu_busy_percent="35", mem_info_gtt_total=str(16 * 2**30), product_name="Radeon RX 6800 XT")
    gpus = detect_sysfs(tmp_path, kfd_path=tmp_path / "nokfd")
    assert len(gpus) == 1
    g = gpus[0]
    assert g.vendor == "amd" and g.name == "Radeon RX 6800 XT"
    assert g.vram_total_mb == 16384 and g.vram_used_mb == 2048 and g.vram_free_mb == 14336
    assert g.utilization_percent == 35 and g.integrated is False
    assert g.compute_apis == [] and "rocm" in g.unavailable
    tel = sample_sysfs(tmp_path)
    assert tel[0].vram_free_mb == 14336


def test_sysfs_amd_apu_detected_as_integrated(tmp_path):
    _fake_card(tmp_path, 0, "0x1002", mem_info_vram_total=str(512 * 2**20), mem_info_vram_used="0",
               mem_info_gtt_total=str(16 * 2**30))
    (tmp_path / "kfd").write_text("")
    g = detect_sysfs(tmp_path, kfd_path=tmp_path / "kfd")[0]
    assert g.integrated is True and g.compute_apis == ["rocm"]


def test_sysfs_intel_igpu_has_no_invented_vram(tmp_path):
    _fake_card(tmp_path, 0, "0x8086")
    g = detect_sysfs(tmp_path)[0]
    assert g.vendor == "intel" and g.integrated is True and g.vram_total_mb is None
    assert "vram_total_mb" in g.unavailable


def test_sysfs_nvidia_without_smi_is_flagged(tmp_path):
    _fake_card(tmp_path, 0, "0x10de")
    g = detect_sysfs(tmp_path)[0]
    assert g.vendor == "nvidia" and g.vram_total_mb is None and "nvidia-smi" in g.unavailable["vram_total_mb"]


def test_merge_prefers_nvidia_smi():
    smi = rows_to_gpus(parse_query_output(RTX3080, QUERY_FIELDS))
    from homemesh.protocol.hardware import GPUInfo

    sysfs = [GPUInfo(index=0, vendor="nvidia", name="NVIDIA", source="sysfs"),
             GPUInfo(index=1, vendor="intel", name="Intel UHD", source="sysfs", integrated=True)]
    merged = _merge_gpus(smi, sysfs)
    assert [(g.vendor, g.source) for g in merged] == [("nvidia", "nvidia-smi"), ("intel", "sysfs")]
    assert [g.index for g in merged] == [0, 1]


def test_windows_gpu_parsing_uses_registry_memory_not_capped_wmi():
    data = {"controllers": [
        {"Name": "AMD Radeon RX 7900 XTX", "AdapterRAM": 4293918720, "DriverVersion": "31.0", "PNPDeviceID": "PCI\\VEN_1002&DEV_744C"},
        {"Name": "NVIDIA GeForce RTX 3080", "AdapterRAM": 4293918720, "PNPDeviceID": "PCI\\VEN_10DE&DEV_2206"},
        {"Name": "Intel(R) UHD Graphics 770", "AdapterRAM": 2147479552, "PNPDeviceID": "PCI\\VEN_8086&DEV_4680"}],
        "registry": [{"Desc": "AMD Radeon RX 7900 XTX", "Mem": 24 * 2**30}]}
    gpus = parse_windows_gpu_json(json.dumps(data))
    assert [g.vendor for g in gpus] == ["amd", "intel"]  # NVIDIA left to nvidia-smi
    assert gpus[0].vram_total_mb == 24576 and gpus[0].integrated is False
    assert gpus[1].integrated is True and gpus[1].vram_total_mb is None
    assert "vram_used_mb" in gpus[0].unavailable


def test_windows_single_controller_object():
    data = {"controllers": {"Name": "AMD Radeon(TM) Graphics", "AdapterRAM": 536870912, "PNPDeviceID": "VEN_1002"}, "registry": {}}
    gpus = parse_windows_gpu_json(json.dumps(data))
    assert gpus[0].vram_total_mb == 512 and gpus[0].integrated is True


def test_macos_apple_silicon_unified_memory():
    data = {"SPDisplaysDataType": [{"sppci_model": "Apple M2 Pro", "spdisplays_vendor": "sppci_vendor_Apple"}]}
    g = parse_system_profiler(json.dumps(data))[0]
    assert g.vendor == "apple" and g.vram_total_mb is None and "unified" in g.unavailable["vram_total_mb"]


def test_os_release_bazzite():
    rel = parse_os_release('NAME="Bazzite"\nID=bazzite\nID_LIKE="fedora"\nVARIANT_ID=bazzite-nvidia\nPRETTY_NAME="Bazzite 41"\n')
    assert rel["ID"] == "bazzite" and rel["PRETTY_NAME"] == "Bazzite 41"


def test_cpuinfo_parsing():
    text = "vendor_id\t: AuthenticAMD\nmodel name\t: AMD Ryzen 9 5900X 12-Core Processor\nflags\t\t: fpu sse avx avx2 fma f16c\n"
    model, vendor, flags = parse_proc_cpuinfo(text)
    assert model.startswith("AMD Ryzen 9") and vendor == "AuthenticAMD" and flags == ["avx", "avx2", "fma", "f16c"]


def test_live_detection_never_fabricates(tmp_path):
    from homemesh.hardware.detect import TelemetrySampler, detect_hardware

    rep = detect_hardware(tmp_path)
    assert rep.memory.total_mb and rep.memory.total_mb > 0
    for g in rep.gpus:  # whatever is present must come from a real source
        assert g.source in ("nvidia-smi", "sysfs", "wmi", "system_profiler")
    t = TelemetrySampler().sample()
    assert t.ram_total_mb == rep.memory.total_mb


def _windows_amd_plus_old_nvidia():
    """Simulated shape of a Windows PC with an RX 5700 (CIM + registry) and a GTX 745 (nvidia-smi)."""
    smi = "0, NVIDIA GeForce GTX 745, GPU-x, 582.78, 4096, 120, 3976, 0, 35, 00000000:02:00.0, 5.0"
    wmi = {"controllers": [
        {"Name": "AMD Radeon RX 5700", "AdapterRAM": 4293918720, "PNPDeviceID": "PCI\\VEN_1002&DEV_731F"},
        {"Name": "NVIDIA GeForce GTX 745", "AdapterRAM": 4293918720, "PNPDeviceID": "PCI\\VEN_10DE&DEV_1382"}],
        "registry": [{"Desc": "AMD Radeon RX 5700", "Mem": 8 * 2**30}]}
    return _merge_gpus(rows_to_gpus(parse_query_output(smi, QUERY_FIELDS)), parse_windows_gpu_json(json.dumps(wmi)))


def test_free_vram_is_unknown_when_only_some_gpus_report_it():
    from homemesh.coordinator.nodes import vram_view
    from homemesh.protocol.hardware import GPUTelemetry, HardwareReport, Telemetry

    gpus = _windows_amd_plus_old_nvidia()
    assert [(g.vendor, g.vram_total_mb) for g in gpus] == [("nvidia", 4096), ("amd", 8192)]
    tel = Telemetry(gpus=[GPUTelemetry(index=0, vram_free_mb=3976, vram_total_mb=4096)])
    total, free, has_gpu = vram_view(HardwareReport.model_construct(gpus=gpus), tel)
    # Only the GTX 745 reports live usage; summing just that would hide the RX 5700's 8 GB.
    assert (total, free, has_gpu) == (12288, None, True)


def test_ignored_gpus_are_shown_but_not_counted():
    from homemesh.coordinator.nodes import vram_view
    from homemesh.hardware.detect import TelemetrySampler, mark_ignored_gpus
    from homemesh.protocol.hardware import GPUTelemetry, HardwareReport, Telemetry

    gpus = _windows_amd_plus_old_nvidia()
    assert mark_ignored_gpus(gpus, ["gtx 745", " "]) == {0}
    assert gpus[0].ignored and not gpus[1].ignored
    tel = Telemetry(gpus=[GPUTelemetry(index=0, vram_free_mb=3976)])
    assert vram_view(HardwareReport.model_construct(gpus=gpus), tel) == (8192, None, True)
    sampler = TelemetrySampler()
    sampler.ignore_indices = {0}
    assert sampler.ignore_indices == {0}
    # Every counted GPU reporting free memory gives a real number again.
    nv = rows_to_gpus(parse_query_output(
        "0, NVIDIA GeForce RTX 3080, GPU-y, 580.1, 10240, 1000, 9240, 3, 40, 00000000:01:00.0, 8.6", QUERY_FIELDS))
    assert vram_view(HardwareReport.model_construct(gpus=nv), Telemetry(gpus=[GPUTelemetry(index=0, vram_free_mb=9000)])) == (10240, 9000, True)
    assert vram_view(HardwareReport.model_construct(gpus=[]), None) == (None, None, False)
