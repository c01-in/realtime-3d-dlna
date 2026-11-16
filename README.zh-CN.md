# Realtime 3D DLNA

**给 3D 投影仪和 3D 电视用的实时 2D 转 3D DLNA 媒体服务器。** 从本地目录或 NAS 点播电影，服务使用 oVDA c16 和 GPU 实时生成双眼画面，输出一路 1920×1080 上下 3D（Top & Bottom / Half TAB）。网页可以查看服务状态、浏览媒体和复制串流地址。

[English](README.md) · [适用场景](#适用场景) · [Linux Compose](#linux-compose) · [Windows 后台安装](#windows-后台安装) · [GPU 资源模式](#gpu-资源模式)

## 适用场景

- **3D 投影仪和家庭影院。** 支持 HDMI 上下 3D 输入的投影仪，可以接一台能通过 DLNA 播放 HEVC 并完整输出 1080p 画面的播放器或电视盒子。播放器点播，投影仪选「上下」模式。具体支持的 3D 模式和 HDMI 输入时序以机型手册为准。[BenQ TK710STi 手册](https://esupportdownload.benq.com/esupport/PROJECTOR%20FOR%20CONSUMER/UserManual/TK710STi/TK710STi_UM_EN_260223154105.pdf) · [Epson Home Cinema 4010 和 Pro Cinema 4050 规格](https://files.support.epson.com/docid/cpd5/cpd55708/source/product_info/concepts/projector_features_hc4010_pc4050.html)
- **3D 电视。** 电视自带支持 HEVC 的 DLNA 播放器时，可以直接浏览片库，也可以接兼容的外置播放器。播放时在电视上选「上下」3D 模式。
- **本地或 NAS 电影片库。** 保留原来的媒体目录，选中一部 2D 电影后按需实时转 3D。服务可以在 Windows 后台或 Linux Docker Compose 内运行。

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
.\windows\Install.ps1 -MediaPath 'D:\Videos' -DataDir 'D:\Realtime3DDLNA' -AcceptOvdaNonCommercial
```

MediaPath 支持多个本地目录和当前账户可读的 UNC 路径。首次运行将安装 Python 依赖并从上游下载模型。DataDir 存放日志和运行文件，ModelDir 可单独指定模型目录。需要添加专用网络防火墙规则时，从管理员 PowerShell 加上 LanFirewall。

## 播放

在播放器里选择 DLNA 服务 **Realtime 3D DLNA**，在投影仪或电视上把 3D 模式选为「上下」。本机可在 http://localhost:8200 打开状态和媒体列表。当前链路保留源帧率，HDR 会映射到 SDR。外挂字幕需要播放器支持。实际速度取决于片源、GPU 和同机负载。

本机 RTX 5080 验收通过。Windows 后台任务从 NAS 连续输出 20 秒 HDR 片段，共解码 500 帧。GPU 空闲时耗时 18.04 秒，有其他 GPU 工作时耗时 25.43 秒。Docker Compose 在本机 Docker Desktop 的 Linux 网络中通过了 SSDP 发现、字幕和音视频解码。另取 4 秒 HDR 片段，3.53 秒内输出并解码了 100 帧。使用 Docker Desktop 时，还需在电视上确认跨出虚拟机的自动发现。

## GPU 资源模式

- **keep-warm（默认）。** 服务启动时加载 oVDA，预热深度推理和立体渲染。服务运行期间，转换进程和模型占用的内存、显存持续保留。
- **release-idle。** 播放首次需要实时转换时才加载模型。最后一个播放请求完成或断开后，连续空闲 30 分钟即退出转换进程，释放模型内存和显存。空闲时仍可通过 DLNA 发现服务和浏览片库。释放后的首次转换需要等待模型加载。

Linux Compose 在 `.env` 中设置 `DLNA_RESOURCE_MODE=release-idle`，再运行 `docker compose up -d`。Windows 安装时加入 `-ResourceMode release-idle`。已经安装的服务可以在 `DataDir` 下的 `settings.json` 中设置 `"resource_mode": "release-idle"`，然后重启计划任务。将值改回 `keep-warm` 即恢复默认。直接运行 `serve.py` 时，可用 `--resource-mode` 选择模式。

`/health` 可查看当前资源模式、转换进程状态与活跃播放请求数。

## 许可

本项目使用 [AGPL-3.0-or-later](LICENSE)。oVDA c16 的代码和权重从海德堡大学及原作者的项目下载，继续使用其独立的 [NC-SA-UHDV1.0 非商业许可](https://huggingface.co/FriedFeid/oVDA/blob/main/LICENSE)。项目同样复用了 [Thru3D Media Server](https://github.com/zerochocobo/Thru3D-Media-Server) 的立体渲染与 DLNA 组件。
