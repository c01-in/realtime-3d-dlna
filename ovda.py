"""Streaming input and playback state for the upstream oVDA c16 model."""

from pathlib import Path
from threading import Lock
from types import MethodType

import numpy as np
import torch
import torch.nn.functional as F
import yaml


def scaled_attention(layer, x, attn_bias=None):
    batch, tokens, channels = x.shape
    packed = layer.qkv(x).reshape(batch, tokens, 3, layer.num_heads, channels // layer.num_heads)
    q, k, v = packed.permute(2, 0, 3, 1, 4).unbind()
    attention = F.scaled_dot_product_attention(q, k, v, attn_mask=attn_bias, scale=layer.scale)
    result = attention.transpose(1, 2).reshape(batch, tokens, channels)
    return layer.proj_drop(layer.proj(result))


class OvdaEngine:
    def __init__(self, upstream: Path, checkpoint: Path):
        from src.models.video_depth import onlineVideoDepthAnything

        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch cannot access a CUDA GPU")
        config = yaml.safe_load((upstream / "configs/oVDA_c16.yaml").read_text(encoding="utf-8"))
        self.model = onlineVideoDepthAnything(**config["net"])
        weights = torch.load(checkpoint, map_location="cpu", weights_only=True)
        self.model.load_state_dict(weights, strict=True)
        self.device = torch.device("cuda:0")
        self.model.to(device=self.device, dtype=torch.float16).eval()
        for block in self.model.pretrained.blocks:
            block.attn.forward = MethodType(scaled_attention, block.attn)
        self.history = self.model.cache_size - 1
        self.mean = torch.tensor((0.485, 0.456, 0.406), device=self.device).view(1, 3, 1, 1)
        self.std = torch.tensor((0.229, 0.224, 0.225), device=self.device).view(1, 3, 1, 1)
        self.lock = Lock()

    def sequence(self, width, height):
        return OvdaSequence(self, width, height)


class OvdaSequence:
    def __init__(self, engine, width, height):
        from src.models.utils.preprocessing import VideoPreprocessor

        self.engine = engine
        ratio = max(width, height) / min(width, height)
        size = round(int(518 * 1.777 / ratio) / 14) * 14 if ratio > 1.78 else 518
        size = max(14, size)
        geometry = VideoPreprocessor(input_size=size, device="cpu", keep_aspect_ratio=True)
        self.width, self.height = (int(n) for n in geometry.get_size(width, height))
        self.cache = None
        self.position = 0

    @torch.inference_mode()
    def __call__(self, rgb):
        engine = self.engine
        with engine.lock:
            if self.cache is None:
                initial = engine.model.setup_cache(self.height, self.width, engine.device)
                self.cache = {key: value.half() for key, value in initial.items()}
                self.masks = [
                    torch.tensor([*range(max(1, pos), engine.history + 1),
                                  *([engine.history] * max(0, pos - 1))], device=engine.device)
                    for pos in range(engine.history + 1)
                ]
            x = torch.from_numpy(rgb.copy()).to(engine.device).permute(2, 0, 1)[None].float().div_(255)
            x = F.interpolate(x, (self.height, self.width), mode="bicubic", align_corners=False)
            x = ((x - engine.mean) / engine.std).half()[:, None]
            position = (self.position, max(1, self.position))
            inverse, appended = engine.model(x, self.cache, self.masks[self.position], position)
            if self.position == engine.history:
                self.cache = {
                    key: torch.cat((value[:, :, :, 1:-1], appended[key], value[:, :, :, -1:]), dim=3)
                    for key, value in self.cache.items()
                }
            else:
                for key, value in self.cache.items():
                    value[:, :, :, self.position:self.position + 1].copy_(appended[key])
                self.position += 1
            # oVDA supplies inverse depth, while the Thru3D renderer accepts depth.
            return inverse[0, 0].float().clamp_min(1e-5).reciprocal().cpu().numpy()
