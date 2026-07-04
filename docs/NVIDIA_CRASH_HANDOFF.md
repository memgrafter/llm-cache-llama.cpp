# NVIDIA Xid 79 / RTX 3090 Ti Crash Handoff

Date: 2026-06-11
Host: `stardart`
GPU: ASUS/NVIDIA RTX 3090 Ti GA102, PCI `0000:01:00.0`, UUID `GPU-e53bc0b5-c645-3f0e-9804-49fb6ac72f2e`
Kernel: Debian `6.12.6-amd64`

## Summary

Repeated GPU failures under llama.cpp burn-in. Failure mode is consistently below userspace: NVIDIA reports **Xid 79, GPU has fallen off the PCIe bus**. After failure, fresh `nvidia-smi` usually fails and PCIe sysfs reports bogus GPU link values (`current_link_speed=Unknown`, `current_link_width=63`, `Bus Type: PCI`). Hot recovery is unlikely; NVIDIA 610 also emits Xid 154 requesting OS reboot.

Driver upgrade from 550.163.01 to 610.43.02 did **not** fix the underlying failure.

## Important findings

### Original 550 driver failures

Driver: `550.163.01`

Three recent crashes:

- Boot -3: `Jun 10 21:08:54` — `NVRM: Xid ... 79 ... GPU has fallen off the bus`
- Boot -2: `Jun 10 21:44:28` — `pcieport 0000:00:01.0: AER: Correctable error ... from 0000:01:00.0`, immediately followed by Xid 79
- Boot -1: `Jun 10 23:40:42` — Xid 79

After failure:

- `nvidia-smi`: `Unable to determine the device handle ... Unknown Error`
- `/proc/driver/nvidia/gpus/.../information`: Video BIOS sometimes `??.??.??.??.??`, Bus Type `PCI`
- `/sys/bus/pci/devices/0000:01:00.0/current_link_speed`: `Unknown`
- `/sys/bus/pci/devices/0000:01:00.0/current_link_width`: `63`

### 610 driver failure

Driver: NVIDIA open kernel module `610.43.02`

Boot current at time of investigation:

```text
NVRM: loading NVIDIA UNIX Open Kernel Module for x86_64 610.43.02
Kernel command line: ... nvidia-drm.modeset=0 video=3840x2160@60
```

Failure at `Jun 11 09:44:44`:

```text
NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.
NVRM: GPU 0000:01:00.0: GPU has fallen off the bus.
NVRM: Xid (PCI:0000:01:00): 154, GPU recovery action changed from 0x0 (None) to 0x2 (OS Reboot)
NVRM: ... GPU lost from the bus [NV_ERR_GPU_IS_LOST]
```

610 produced lots of GSP/RM fallout:

- `kgmmuInvalidateTlb_GM107: TLB invalidation failed ... status=0x0000000f`
- `rpcSendMessage failed`
- `GspRmFree failed`
- `mmuWalkUnmap failed`
- `NV_ERR_INVALID_STATE`
- `NV_ERR_GPU_IS_LOST`

`llama-server` aborted/coredumped right after the GPU failure.

## Workload

llama.cpp stack / benchmark:

- `llama-server` with model `Qwopus3.6-27B-v2-MTP-Q4_K_M.gguf`
- `--ctx-size 128000`
- `--gpu-layers 999`
- batch/ubatch commonly `4096/1024`
- MTP draft enabled
- ~22–23 GiB VRAM usage

Relevant logs live under:

```text
/home/robbintt/code/llm-cache-llama.cpp/logs/
```

Example 610 burn-in logs:

```text
qwen36-backend-qwopus27-v2-mtp-q4km-128k-mtp3-nonthinking-burnin-repeat-20260611-093119.log
lmcache-proxy-qwopus27-v2-mtp-q4km-128k-mtp3-nonthinking-burnin-repeat-20260611-093119.log
stack-qwopus27-v2-mtp-q4km-128k-mtp3-nonthinking-burnin-repeat-20260611-093119.log
```

## Monitoring commands used

GPU telemetry:

```bash
nvidia-smi --query-gpu=timestamp,name,pci.bus_id,driver_version,temperature.gpu,utilization.gpu,utilization.memory,power.draw,clocks_throttle_reasons.active --format=csv -l 1 | tee nvidia-smi-burnin.csv
```

Kernel GPU logs:

```bash
journalctl -k -f | grep -Ei 'NVRM|Xid|nvidia|GPU|pcie|AER|fallen|kgmmu|thermal|power|error|warn' | tee kernel-gpu-burnin.log
```

Thermal/power minimal:

```bash
nvidia-smi --query-gpu=timestamp,temperature.gpu,temperature.memory,power.draw,clocks_throttle_reasons.active --format=csv -l 1 | tee gpu-temps-power.csv
```

Note: `temperature.memory` reports `N/A` on this setup.

Quick status checks:

```bash
nvidia-smi
journalctl -k -b 0 --no-pager | grep -Ei 'NVRM: Xid|GPU has fallen|AER:.*01:00|GPU lost|kgmmu|thermal|power' | tail -80
for d in 0000:00:01.0 0000:01:00.0; do echo $d; for f in current_link_speed current_link_width max_link_speed max_link_width; do printf '%s=' $f; cat /sys/bus/pci/devices/$d/$f 2>/dev/null; done; done
```

## PCIe forcing

BIOS was used to force PCIe Gen3. Gen3 still failed.

Observed in fail state under Gen3:

```text
0000:00:01.0 max_link_speed=8.0 GT/s PCIe
0000:01:00.0 current_link_speed=Unknown
0000:01:00.0 current_link_width=63
```

Conclusion: forcing Gen3 did not prevent failure, so pure high-speed PCIe signal integrity is less likely than before, though slot/contact/board electrical issues still possible.

## Power-limit testing

Set cap:

```bash
sudo nvidia-smi -pl 250
sudo nvidia-smi -pl 300
```

Reset default likely:

```bash
sudo nvidia-smi -pl 450
```

### 250W result

250W burn-in completed successfully.

Post-test:

```text
nvidia-smi works
44C idle
26W / 250W
~23376 MiB VRAM in use
No Xid/GPU-lost/AER lines in current boot check
```

This strongly suggests the failure threshold is above 250W and points toward power/transient/VRM/thermal-margin rather than software alone.

### 300W result

At 300W cap, actual draw was about `293W` / sometimes ~300W.

Throttle reason bits returned to the problematic pattern:

- frequent `0x20`
- some `0x68`

Similar to pre-cap failures.

Interpretation: 300W enters same bad regime; 250W looked clean. Threshold likely between 250W and 300W.

## Throttle bit notes

Field: `clocks_throttle_reasons.active`

Observed:

- `0x01` = idle / GPU idle
- `0x04` = SW power cap (expected when using `nvidia-smi -pl`)
- `0x20` = SW thermal slowdown
- `0x40` = HW thermal slowdown
- `0x08` = HW slowdown
- `0x68` = `0x40 + 0x20 + 0x08`

Important: Core temp was only ~70–72C during failures. If thermal bits are meaningful, suspect hotspot/VRAM/VRM/power-stage temps rather than core temp. Memory temp is unavailable via `nvidia-smi` here.

## Current diagnosis

Most likely buckets, in rough order after Gen3 and power-cap tests:

1. GPU/card power delivery or transient current instability
2. GPU VRM / hotspot / memory thermal margin issue
3. PSU / 12VHPWR / PCIe power cable or connector issue
4. GPU hardware fault
5. Motherboard slot / board electrical issue
6. Driver bug alone: less likely, because 550 and 610 both reproduce Xid 79 under load

250W stability is the strongest new evidence. 300W showing `0x20/0x68` suggests the card enters a thermal/hardware slowdown regime before failure.

## Suggested next tests

- Try intermediate caps: `275W`, maybe `285W`.
- Pick highest cap with no Xid, no frequent `0x68`, minimal/predictable `0x20`.
- Repeat 300W with external fan aimed at GPU/backplate. If `0x20/0x68` decrease or it stops crashing, thermal/VRM cooling is implicated.
- Check/replace/reseat 12VHPWR and PSU-side connectors; avoid adapters if possible.
- If willing, inspect GPU pads/paste/backplate thermal path. For 3090 Ti, VRAM/VRM pads or putty may matter more than core paste.
- Consider testing in another slot/system or with another PSU if available.

## Hot recovery

Not reliable. After Xid 79 + bad PCIe state, NVIDIA 610 explicitly says OS reboot. A full power cycle may be needed if warm reboot does not clear the card.
