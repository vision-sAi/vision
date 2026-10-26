"""Int8 weight-only linears for the Qwen3-TTS talker on CUDA.

Every `nn.Linear` in the talker and code-predictor transformer layers keeps its weights as int8 with one
fp32 scale per output channel (2.6 GB of bf16 becomes 1.3 GB). The talker is memory-bandwidth-bound while
it speaks (one pass over all weights per 80 ms frame), so reading half the bytes is also faster. Decode
steps (a handful of rows) use a small Triton kernel that multiplies straight from int8; prefill (hundreds
of rows) dequantises the layer to bf16 and lets cuBLAS do it. Activations, KV cache and sampling stay bf16,
so this is the near-lossless kind of quantisation; Whisper transcribes the output word for word.
"""
from __future__ import annotations

import threading
import time

import torch
import torch.nn as nn
import torch.nn.functional as F

try:
    import triton
    import triton.language as tl
except ImportError:  # pragma: no cover - CPU-only installs
    triton = tl = None

if triton is not None:

    @triton.jit
    def _gemv_i8(x_ptr, w_ptr, s_ptr, y_ptr, M, N, K, stride_xm, stride_wn, stride_ym,
                 BLOCK_N: tl.constexpr, BLOCK_K: tl.constexpr, BLOCK_M: tl.constexpr):
        """y[m, n] = scale[n] * sum_k x[m, k] * w[n, k] for m < M (M <= BLOCK_M); w int8, x and y bf16."""
        pid = tl.program_id(0)
        rn = pid * BLOCK_N + tl.arange(0, BLOCK_N)
        n_ok = rn < N
        for m in tl.static_range(BLOCK_M):
            acc = tl.zeros((BLOCK_N,), dtype=tl.float32)
            for k0 in range(0, K, BLOCK_K):
                rk = k0 + tl.arange(0, BLOCK_K)
                k_ok = rk < K
                w = tl.load(w_ptr + rn[:, None] * stride_wn + rk[None, :], mask=n_ok[:, None] & k_ok[None, :], other=0).to(tl.float32)
                x = tl.load(x_ptr + m * stride_xm + rk, mask=k_ok & (m < M), other=0.0).to(tl.float32)
                acc += tl.sum(w * x[None, :], axis=1)
            s = tl.load(s_ptr + rn, mask=n_ok, other=0.0)
            tl.store(y_ptr + m * stride_ym + rn, (acc * s).to(tl.bfloat16), mask=n_ok & (m < M))


class Int8Linear(nn.Module):
    """Drop-in for an `nn.Linear` whose weights are int8 with a per-output-channel scale."""

    GEMV_ROWS = 4  # up to this many input rows use the Triton kernel; more dequantise and use cuBLAS
    BLOCK_N, BLOCK_K = 16, 512  # tuned for the talker's shapes

    def __init__(self, lin: nn.Linear):
        super().__init__()
        w = lin.weight.detach()
        scale = w.abs().amax(dim=1).float().clamp_min(1e-8) / 127.0
        q = torch.round(w.float() / scale[:, None]).clamp(-127, 127).to(torch.int8)
        self.register_buffer("weight", q.contiguous())  # [out, in] int8
        self.register_buffer("scale", scale.contiguous())  # [out] fp32
        self.bias = lin.bias
        self.in_features, self.out_features = lin.in_features, lin.out_features

    def extra_repr(self) -> str:
        return f"in_features={self.in_features}, out_features={self.out_features}, int8"

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        shape = x.shape
        x2 = x.reshape(-1, shape[-1])
        rows, k = x2.shape
        n = self.out_features
        if rows <= self.GEMV_ROWS and x.is_cuda and triton is not None:
            x2 = x2.contiguous()
            y = torch.empty(rows, n, dtype=torch.bfloat16, device=x.device)
            _gemv_i8[(triton.cdiv(n, self.BLOCK_N),)](
                x2, self.weight, self.scale, y, rows, n, k, x2.stride(0), self.weight.stride(0), y.stride(0),
                BLOCK_N=self.BLOCK_N, BLOCK_K=self.BLOCK_K, BLOCK_M=self.GEMV_ROWS, num_warps=4,
            )
        else:
            w = (self.weight.float() * self.scale[:, None]).to(x.dtype)
            y = F.linear(x2, w)
        if self.bias is not None:
            y = y + self.bias
        return y.reshape(*shape[:-1], n)


def quantize_linears(module: nn.Module) -> int:
    """Replace every nn.Linear under `module` (in place) with an Int8Linear; returns how many."""
    n = 0
    for name, child in list(module.named_children()):
        if isinstance(child, nn.Linear):
            setattr(module, name, Int8Linear(child))
            n += 1
        else:
            n += quantize_linears(child)
    return n


def quantize_talker(core) -> int:
    """Shrink the talker and code-predictor transformer layers of a loaded Qwen3-TTS `core` to int8."""
    return quantize_linears(core.talker.model.layers) + quantize_linears(core.talker.code_predictor.model.layers)


def move_stray_tensors(module: nn.Module, device) -> int:
    """Move tensors that modules keep as plain attributes (not parameters or buffers, so `.to()` skips
    them) to `device`; the codec's vector-quantiser codebooks are such tensors. Returns how many moved."""
    n = 0
    device = torch.device(device)
    for m in module.modules():
        for name, val in list(vars(m).items()):
            if torch.is_tensor(val) and val.device != device:
                setattr(m, name, val.to(device))
                n += 1
    return n


if triton is not None:

    @triton.jit
    def _spin(out_ptr, ITERS: tl.constexpr):
        """Burn a few milliseconds on one SM without touching memory (see ClockHold)."""
        x = tl.zeros((128,), dtype=tl.float32) + tl.program_id(0)
        for _ in range(ITERS):
            x = x * 0.9999 + 0.0001
        tl.store(out_ptr + tl.arange(0, 128), x)


class ClockHold:
    """Keep the GPU's clock governor in its performance state while the talker speaks.

    The int8 talker's kernels are short and sparse (the code predictor is 5,000 tiny launches a frame),
    and the machine driver reads that as a light load: it drops to P2 (1.5 GHz, 11 GHz memory), which
    costs more than int8 saves. One short register-spinning kernel
    kept queued on the lowest-priority stream makes the load look continuous, so the clocks stay at P0.
    It occupies a sliver of one SM, no memory bandwidth, and a small amount of CPU time.
    """

    ITERS = 2_000_000  # calibrated for a short launch
    _ms: float | None = None

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if triton is None or not torch.cuda.is_available():
            return self
        out = torch.zeros(128, device="cuda")
        if ClockHold._ms is None:
            _spin[(1,)](out, ITERS=self.ITERS)
            s, e = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
            s.record()
            _spin[(1,)](out, ITERS=self.ITERS)
            e.record()
            e.synchronize()
            ClockHold._ms = max(0.5, s.elapsed_time(e))
        period = ClockHold._ms * 0.8 / 1000  # keep one launch queued, never let the queue run dry
        lo, hi = torch.cuda.Stream.priority_range()
        stream = torch.cuda.Stream(priority=max(lo, hi))  # the largest number is the lowest priority

        def run():
            with torch.cuda.stream(stream):
                while not self._stop.is_set():
                    _spin[(1,)](out, ITERS=self.ITERS)
                    time.sleep(period)
            stream.synchronize()

        self._thread = threading.Thread(target=run, daemon=True, name="gpu-clock-hold")
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
