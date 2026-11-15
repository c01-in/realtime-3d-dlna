<div align="center">

# Realtime 3D DLNA

**Play your 2D library in 3D on the TV.**

On-demand oVDA c16 depth, GPU stereo rendering and a single 1080p top-and-bottom DLNA stream.

[![App license](https://img.shields.io/badge/app_license-AGPL--3.0--or--later-14B8A6?style=flat-square)](LICENSE)
[![Output](https://img.shields.io/badge/output-1080p_Top%E2%80%93Bottom-58A6FF?style=flat-square)](#playback)

[简体中文](README.zh-CN.md) · [Linux Compose](#linux-compose) · [Windows](#windows) · [Model license](#license)

</div>

Open the web dashboard to see the service status, walk the media folders and copy a playback URL. Your TV can also discover the service directly using DLNA. The server renders a video only while a client is requesting it, with byte ranges for seeking and DLNA sidecar subtitle metadata.

## Requirements

- NVIDIA CUDA GPU and a driver compatible with the runtime. The FFmpeg encoder uses HEVC NVENC. NVDEC is used for supported inputs, with software decoding available when hardware decoding cannot start.
- A local media directory or a NAS directory mounted or readable by the service account. The TV and server share a trusted LAN.
- Windows uses Python 3.12 and an FFmpeg build with zscale and NVENC. Linux uses Docker Engine, Compose and the NVIDIA Container Toolkit.

Realtime speed depends on the source, GPU and competing GPU workloads. Use a real playback test with the target media before relying on a hardware configuration. The service exposes HTTP on TCP 8200 by default and discovers devices using SSDP on UDP 1900.

## Linux Compose

Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the Docker host. Clone this repo, copy the environment template and set MEDIA_DIR to your library. A NAS mount works as the source of the same read-only bind mount.

```sh
git clone https://github.com/c01-in/realtime-3d-dlna.git
cd realtime-3d-dlna
cp .env.example .env
# Edit .env and set MEDIA_DIR before running Compose.
docker compose build
docker compose run --rm dlna python assets.py --data-dir /data --accept-ovda-noncommercial
docker compose up -d
docker compose logs -f dlna
```

The assets command downloads the pinned upstream sources and validates the c16 checkpoint. The image is built locally. Set DATA_DIR in .env to put the persistent model and GPU cache on another volume. GPU_DEVICE selects a GPU on hosts with multiple cards. The default host network is for Linux Docker Engine. On Windows Docker Desktop, test SSDP on the TV separately because Docker Desktop handles host networking differently.

## Windows

Install [Python 3.12](https://www.python.org/downloads/) and an [FFmpeg build](https://ffmpeg.org/download.html) with HEVC NVENC and zscale. Run the installer from the cloned repo. It creates a per-user Python environment, installs the runtime assets and registers a Task Scheduler task that runs as the signed-in user via pythonw.exe.

```powershell
.windowsInstall.ps1 -MediaPath 'D:Videos' -DataDir 'D:Realtime3DDLNA' -AcceptOvdaNonCommercial
```

Pass multiple values to MediaPath to expose multiple folders, including UNC paths that the Windows account can read. Optional parameters include Port, LanIp and FfmpegDir. Run with LanFirewall from an elevated session if you want the installer to add inbound rules for a trusted Private network. Specify PythonExe plus ExtraPythonPath to reuse an existing compatible environment. The service logs to server.log under DataDir.

To stop the service, use Stop-ScheduledTask -TaskName 'Realtime 3D DLNA'. To uninstall the scheduled entry, stop it and use Unregister-ScheduledTask -TaskName 'Realtime 3D DLNA'. Your library and runtime data remain available to you.

## Playback

Open the TV or player’s DLNA sources and select **Realtime 3D DLNA**. The picture-mode setting on the TV is **Top and Bottom**. Video keeps the source frame rate and is streamed as 1920×1080 HEVC with AAC when the input has audio. HDR sources are tone-mapped to SDR. External subtitles depend on the player supporting DLNA sidecar captions.

Open http://localhost:8200 on the server for the dashboard. The health endpoint is /health. Use tests/verify.py with a video title to check discovery, browsing, HTTP byte ranges and actual decode. The script accepts repeated --directory for nested folders.

### Local validation

On an RTX 5080, the Windows scheduled service returned 500 decoded HEVC frames from a 20-second HDR NAS excerpt. The transfer took 18.04 seconds with no other substantial GPU workload and 25.43 seconds with competing GPU activity. Docker Compose on the same machine passed CUDA rendering, HEVC/AAC decode, sidecar subtitles and SSDP from within Docker Desktop's Linux network. A four-second HDR excerpt returned 100 frames in 3.53 seconds. Test SSDP from your TV when using Docker Desktop.

## License

This project is [AGPL-3.0-or-later](LICENSE), including the service code derived from [Thru3D Media Server](https://github.com/zerochocobo/Thru3D-Media-Server). The setup fetches the oVDA c16 code and checkpoint from Heidelberg University and the original authors. Those assets retain their separate [NC-SA-UHDV1.0 terms](https://huggingface.co/FriedFeid/oVDA/blob/main/LICENSE) for non-commercial use, with commercial licensing available from the rights holder. See [the upstream oVDA project](https://github.com/FriedFeid/OnlineVideoDepthAnything) for attribution and model details.
