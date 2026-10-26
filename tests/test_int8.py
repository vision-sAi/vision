import unittest

try:
    import pytest
    import torch
except ImportError:  # the voice extra and pytest aren't installed
    raise unittest.SkipTest("needs pytest and torch")

cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a CUDA GPU")


@cuda
def test_int8_linear_matches_bf16_and_captures():
    from vision.int8 import Int8Linear, quantize_linears

    torch.manual_seed(0)
    lin = torch.nn.Linear(2048, 6144, bias=False, dtype=torch.bfloat16, device="cuda")
    q = Int8Linear(lin)
    assert q.weight.dtype == torch.int8 and q.weight.shape == (6144, 2048)
    for rows in (1, 2, 4, 37):  # kernel path up to 4 rows, dequantised cuBLAS beyond
        x = torch.randn(1, rows, 2048, dtype=torch.bfloat16, device="cuda")
        ref, out = lin(x).float(), q(x).float()
        assert ((out - ref).norm() / ref.norm()).item() < 0.01
    # A CUDA graph can capture the kernel path and replays with fresh input.
    x = torch.randn(1, 1, 2048, dtype=torch.bfloat16, device="cuda")
    s = torch.cuda.Stream()
    s.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(s):
        for _ in range(3):
            q(x)
    torch.cuda.current_stream().wait_stream(s)
    g = torch.cuda.CUDAGraph()
    with torch.cuda.graph(g):
        y = q(x)
    x.copy_(torch.randn_like(x))
    g.replay()
    torch.cuda.synchronize()
    ref = lin(x).float()
    assert ((y.float() - ref).norm() / ref.norm()).item() < 0.01
    # quantize_linears swaps nested linears in place and counts them.
    m = torch.nn.Sequential(torch.nn.Linear(8, 8), torch.nn.Sequential(torch.nn.Linear(8, 4)))
    assert quantize_linears(m) == 2 and isinstance(m[1][0], Int8Linear)


def test_quantize_on_cpu_then_move():
    """Quantising happens on the CPU before the move to the GPU; the buffers must follow the module."""
    from vision.int8 import Int8Linear

    lin = torch.nn.Linear(16, 8, dtype=torch.bfloat16)
    q = Int8Linear(lin)
    x = torch.randn(3, 16, dtype=torch.bfloat16)
    ref = lin(x).float()
    out = q(x).float()  # CPU forward takes the dequantise path
    assert ((out - ref).norm() / ref.norm()).item() < 0.02
    if torch.cuda.is_available():
        q.to("cuda")
        assert q.weight.is_cuda and q.scale.is_cuda
