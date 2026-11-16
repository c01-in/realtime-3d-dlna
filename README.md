<div align="center">

# Realtime 3D DLNA

**Real-time 2D-to-3D streaming for 3D projectors and 3D TVs.**

A DLNA media server for home theater. Stream local or NAS movies in 1080p Top & Bottom (Half TAB) 3D with oVDA c16 and GPU stereo rendering.

[![App license](https://img.shields.io/badge/app_license-AGPL--3.0--or--later-14B8A6?style=flat-square)](LICENSE)
[![Output](https://img.shields.io/badge/output-1080p_Top%E2%80%93Bottom-58A6FF?style=flat-square)](#playback)

[简体中文](README.zh-CN.md) · [Use cases](#use-cases) · [Linux Compose](#linux-compose) · [Windows](#windows) · [GPU resource mode](#gpu-resource-mode) · [Model license](#license)

</div>

Open the web dashboard to see the service status, walk the media folders and copy a playback URL. A compatible DLNA player can discover the server on the local network. The server renders a video only while a client is requesting it, with byte ranges for seeking and DLNA sidecar subtitle metadata.

## Use cases

- **3D projector home theater.** If your projector accepts Top & Bottom 3D over HDMI, connect a DLNA player or streaming box that can decode HEVC and output the full 1080p frame. Select Top & Bottom on the projector. Look for this mode and the supported HDMI timings in your projector's manual. [BenQ TK710STi manual](https://esupportdownload.benq.com/esupport/PROJECTOR%20FOR%20CONSUMER/UserManual/TK710STi/TK710STi_UM_EN_260223154105.pdf) · [Epson Home Cinema 4010 and Pro Cinema 4050 specifications](https://files.support.epson.com/docid/cpd5/cpd55708/source/product_info/concepts/projector_features_hc4010_pc4050.html)
- **3D TV playback.** Browse the server in the TV's built-in DLNA player if it supports HEVC, or connect a compatible external player. Enable the TV's Top & Bottom mode to view the two eyes in 3D.
- **Local and NAS movie libraries.** Browse your existing folders and pick a 2D movie to convert on demand. Run the GPU server in the background on Windows or with Docker Compose on Linux.

## Requirements

- NVIDIA CUDA GPU and a driver compatible with the runtime. The FFmpeg encoder uses HEVC NVENC. NVDEC is used for supported inputs, with software decoding available when hardware decoding cannot start.
- A local media directory or a NAS directory mounted or readable by the service account. The DLNA player and server share a trusted LAN.
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
.\windows\Install.ps1 -MediaPath 'D:\Videos' -DataDir 'D:\Realtime3DDLNA' -AcceptOvdaNonCommercial
```

Pass multiple values to MediaPath to expose multiple folders, including UNC paths that the Windows account can read. Optional parameters include Port, LanIp and FfmpegDir. Run with LanFirewall from an elevated session if you want the installer to add inbound rules for a trusted Private network. Specify PythonExe plus ExtraPythonPath to reuse an existing compatible environment. The service logs to server.log under DataDir.

To stop the service, use Stop-ScheduledTask -TaskName 'Realtime 3D DLNA'. To uninstall the scheduled entry, stop it and use Unregister-ScheduledTask -TaskName 'Realtime 3D DLNA'. Your library and runtime data remain available to you.

## Playback

Open the player's DLNA sources and select **Realtime 3D DLNA**. On the projector or TV, select **Top & Bottom** (also called **Over-Under**). Video keeps the source frame rate and is streamed as 1920×1080 HEVC with AAC when the input has audio. HDR sources are tone-mapped to SDR. External subtitles depend on the player supporting DLNA sidecar captions.

Open http://localhost:8200 on the server for the dashboard. The health endpoint is /health. Use tests/verify.py with a video title to check discovery, browsing, HTTP byte ranges and actual decode. The script accepts repeated --directory for nested folders.

### Local validation

On an RTX 5080, the Windows scheduled service returned 500 decoded HEVC frames from a 20-second HDR NAS excerpt. The transfer took 18.04 seconds with no other substantial GPU workload and 25.43 seconds with competing GPU activity. Docker Compose on the same machine passed CUDA rendering, HEVC/AAC decode, sidecar subtitles and SSDP from within Docker Desktop's Linux network. A four-second HDR excerpt returned 100 frames in 3.53 seconds. Test SSDP from your TV when using Docker Desktop.

## GPU resource mode

- **keep-warm (default).** Load oVDA and run a depth and stereo warm-up at startup. Keep the GPU worker, model RAM and VRAM resident while the service runs.
- **release-idle.** Start the GPU worker when playback first needs to generate a segment. The worker exits after 30 minutes of continuous inactivity after the final playback request completes or disconnects, freeing the model's RAM and VRAM. DLNA discovery and browsing stay available. The first conversion after release takes longer.

For Linux Compose, set `DLNA_RESOURCE_MODE=release-idle` in `.env` and run `docker compose up -d`. For Windows, pass `-ResourceMode release-idle` to the installer, or set `"resource_mode": "release-idle"` in the existing `settings.json` under `DataDir` and restart the scheduled task. Use `keep-warm` in those settings to restore the default. When running `serve.py` directly, use `--resource-mode` to select either mode.

The `/health` endpoint reports the selected resource mode, worker state and active playback request count.

## License

This project is [AGPL-3.0-or-later](LICENSE), including the service code derived from [Thru3D Media Server](https://github.com/zerochocobo/Thru3D-Media-Server). The setup fetches the oVDA c16 code and checkpoint from Heidelberg University and the original authors. Those assets retain their separate [NC-SA-UHDV1.0 terms](https://huggingface.co/FriedFeid/oVDA/blob/main/LICENSE) for non-commercial use, with commercial licensing available from the rights holder. See [the upstream oVDA project](https://github.com/FriedFeid/OnlineVideoDepthAnything) for attribution and model details.
