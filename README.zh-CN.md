# Realtime 3D DLNA

**在电视上实时播放上下 3D。** 选一个本地目录或 NAS 目录，电视通过 DLNA 点播。服务使用 oVDA c16 和 GPU 实时生成双眼画面，输出一路 1920×1080 上下 3D。网页可以查看服务状态、浏览媒体和复制串流地址。

[English](README.md) · [Linux Compose](#linux-compose) · [Windows 后台安装](#windows-后台安装)

## Linux Compose

主机需要 NVIDIA CUDA GPU、支持 HEVC NVENC 的驱动、Docker Compose 和 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。

```bash
git clone https://github.com/c01-in/realtime-3d-dlna.git
cd realtime-3d-dlna
cp .env.example .env
# 在 .env 中把 MEDIA_DIR 改为本地目录或已经挂载的 NAS 路径。
docker compose build
docker compose run --rm dlna python assets.py --data-dir /data --accept-ovda-noncommercial
docker compose up -d
```

DATA_DIR 可指向其他磁盘。GPU_DEVICE 选择主机上的 GPU。Compose 默认使用 Linux 宿主网络。Windows Docker Desktop 的网络实现不同，使用它时单独验证电视能否自动发现服务。

## Windows 后台安装

先装 Python 3.12 和支持 HEVC NVENC、zscale 的 FFmpeg，在项目目录运行下面的 PowerShell 命令。计划任务会在当前账户登录后以 pythonw.exe 启动，播放时无需打开终端。

```powershell
.windowsInstall.ps1 -MediaPath 'D:Videos' -DataDir 'D:Realtime3DDLNA' -AcceptOvdaNonCommercial
```

MediaPath 支持多个本地目录和当前账户可读的 UNC 路径。首次运行将安装 Python 依赖并从上游下载模型。DataDir 存放日志和运行文件，ModelDir 可单独指定模型目录。需要添加专用网络防火墙规则时，从管理员 PowerShell 加上 LanFirewall。

电视选择 DLNA 服务 **Realtime 3D DLNA**，3D 模式选「上下」。本机可在 http://localhost:8200 打开状态和媒体列表。当前链路保留源帧率，HDR 会映射到 SDR。外挂字幕需要播放器支持。实际速度取决于片源、GPU 和同机负载。

本机 RTX 5080 验收通过。Windows 后台任务从 NAS 连续输出 20 秒 HDR 片段，共解码 500 帧。GPU 空闲时耗时 18.04 秒，有其他 GPU 工作时耗时 25.43 秒。Docker Compose 在本机 Docker Desktop 的 Linux 网络中通过了 SSDP 发现、字幕和音视频解码。另取 4 秒 HDR 片段，3.53 秒内输出并解码了 100 帧。使用 Docker Desktop 时，还需在电视上确认跨出虚拟机的自动发现。

## 许可

本项目使用 [AGPL-3.0-or-later](LICENSE)。oVDA c16 的代码和权重从海德堡大学及原作者的项目下载，继续使用其独立的 [NC-SA-UHDV1.0 非商业许可](https://huggingface.co/FriedFeid/oVDA/blob/main/LICENSE)。项目同样复用了 [Thru3D Media Server](https://github.com/zerochocobo/Thru3D-Media-Server) 的立体渲染与 DLNA 组件。
