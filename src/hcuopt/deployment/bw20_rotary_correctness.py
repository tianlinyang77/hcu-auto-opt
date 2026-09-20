# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bounded rotary diagnostic. Run on HCU only after reviewing its container scope.

No model serving, benchmark, rebuild or release decision. The CPU mathematical
reference does not call the operator or its Python reference implementation.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import struct
import sys
from pathlib import Path

PACKAGE_VERSION = "0.4.2.post2+das.opt1.dtk2604.torch2100.2606021957.gdad582"
BINARY = Path("/usr/local/lib/python3.10/dist-packages/sgl_kernel/"
              "common_ops.cpython-310-x86_64-linux-gnu.so")
BINARY_SHA256 = "6c1cb45465cff84b6563fa930a73a2da6e4091539c17d55291a765864944291e"
ATOL = RTOL = 1 / 128
TOKENS = (1, 8, 248, 256)
SEEDS = (0, 17)
PREFIX = "HCUOPT_ROTARY_JSON "


def emit(value: dict) -> None:
    print(PREFIX + json.dumps(value, allow_nan=False, sort_keys=True), flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def f32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def reference_head(values: list[float], cache: list[float]) -> list[float]:
    """NeoX half-split rotation, FP32 arithmetic on exactly the BF16 inputs."""
    if len(values) != len(cache) or len(values) % 2:
        raise ValueError("reference requires equal, even head/cache lengths")
    half = len(values) // 2
    left, right = [], []
    for index in range(half):
        x, y = values[index], values[index + half]
        c, s = cache[index], cache[index + half]
        left.append(f32(f32(x * c) - f32(y * s)))
        right.append(f32(f32(y * c) + f32(x * s)))
    return left + right


def metrics(actual: list[float], expected: list[float]) -> dict:
    if not actual or len(actual) != len(expected):
        raise ValueError("missing or mismatched numerical observations")
    if not all(math.isfinite(v) for v in actual + expected):
        return {"passed": False, "reason": "nonfinite_value"}
    absolute = [abs(a - b) for a, b in zip(actual, expected, strict=True)]
    relative = [error / max(abs(b), 1e-12) for error, b in zip(absolute, expected, strict=True)]
    failures = [i for i, (error, b) in enumerate(zip(absolute, expected, strict=True))
                if error > ATOL + RTOL * abs(b)]
    return {
        "passed": not failures, "count": len(actual), "mismatch_count": len(failures),
        "max_abs": max(absolute), "mean_abs": sum(absolute) / len(absolute),
        "max_rel": max(relative), "mean_rel": sum(relative) / len(relative),
        "rmse": math.sqrt(sum(e * e for e in absolute) / len(absolute)),
        "first_mismatches": [{"index": i, "actual": actual[i], "reference": expected[i]}
                             for i in failures[:16]],
    }


def tensor_hash(torch, value) -> str:
    raw = value.detach().cpu().contiguous().view(torch.uint8).reshape(-1).tolist()
    return hashlib.sha256(bytes(raw)).hexdigest()


def run_case(torch, operator, cache, cache_cpu, *, tokens: int, seed: int, padded: bool) -> bool:
    width = 72 if padded else 64
    q_store = torch.full((tokens + 2, 14, width), 7, dtype=torch.bfloat16, device="cuda:0")
    k_store = torch.full((tokens + 2, 2, width), 7, dtype=torch.bfloat16, device="cuda:0")
    q, k = q_store[1:-1, :, :64], k_store[1:-1, :, :64]
    positions = torch.zeros(tokens, dtype=torch.int64, device="cuda:0")
    pointers = (q.data_ptr(), k.data_ptr())
    cache_hash = tensor_hash(torch, cache)

    def reset(phase: int):
        q_cpu = torch.tensor([math.sin(i * .173 + seed + phase) for i in range(tokens * 14 * 64)],
                             dtype=torch.bfloat16).reshape(tokens, 14, 64)
        k_cpu = torch.tensor([math.cos(i * .217 + seed + phase) for i in range(tokens * 2 * 64)],
                             dtype=torch.bfloat16).reshape(tokens, 2, 64)
        # Include exactly zero, constant extrema and near-zero input values.
        q_cpu.reshape(-1)[:4] = torch.tensor([0, 1, -1, 1e-4], dtype=torch.bfloat16)
        pos = [(i * 37 + seed + phase * 101) % 32768 for i in range(tokens)]
        pos[0] = 0
        if tokens > 1:
            pos[-1] = 32767
        if tokens > 2:
            pos[1] = 0
        q.copy_(q_cpu)
        k.copy_(k_cpu)
        positions.copy_(torch.tensor(pos, dtype=torch.int64))
        refs = []
        for source in (q_cpu, k_cpu):
            refs.append([number for token, heads in enumerate(source.tolist())
                         for head in heads for number in reference_head(
                             head, cache_cpu[pos[token]].tolist())])
        return refs, pos, (tensor_hash(torch, q_cpu), tensor_hash(torch, k_cpu))

    def invoke():
        operator(positions, q, k, 64, cache, True)

    def check(mode: str, replay: int, refs, pos, input_hashes):
        torch.cuda.synchronize()
        q_result, k_result = q.cpu(), k.cpu()
        q_stats = metrics(q_result.reshape(-1).float().tolist(), refs[0])
        k_stats = metrics(k_result.reshape(-1).float().tolist(), refs[1])
        guards = all(bool((storage[[0, -1]] == 7).all().item())
                     and (not padded or bool((storage[:, :, 64:] == 7).all().item()))
                     for storage in (q_store, k_store))
        side_effects = (guards and positions.cpu().tolist() == pos
                        and tensor_hash(torch, cache) == cache_hash
                        and (q.data_ptr(), k.data_ptr()) == pointers)
        passed = q_stats["passed"] and k_stats["passed"] and side_effects
        emit({"event": "case", "tokens": tokens, "seed": seed, "padded": padded,
              "mode": mode, "replay": replay, "q": q_stats, "k": k_stats,
              "side_effect_checks_passed": side_effects, "passed": passed,
              "q_stride": list(q.stride()), "k_stride": list(k.stride()),
              "input_sha256": input_hashes, "positions": pos,
              "output_sha256": [tensor_hash(torch, q_result), tensor_hash(torch, k_result)]})
        return passed

    refs, pos, input_hashes = reset(0)
    invoke()
    if not check("eager", 0, refs, pos, input_hashes):
        return False
    stream = torch.cuda.Stream()
    stream.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(stream):
        reset(0)
        invoke()
    torch.cuda.synchronize()
    reset(0)
    torch.cuda.synchronize()
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph, stream=stream):
        invoke()
    for replay in (1, 2):
        # Different data/positions at the same addresses detect stale graph inputs.
        refs, pos, input_hashes = reset(replay)
        graph.replay()
        if not check("graph", replay, refs, pos, input_hashes):
            return False
    del graph
    return True


def main() -> int:
    emit({"event": "protocol", "schema": "bw20-rotary-numerics-v1",
          "tokens": TOKENS, "seeds": SEEDS, "q_heads": 14, "kv_heads": 2,
          "head_dim": 64, "dtype": "bfloat16", "is_neox": True,
          "atol": ATOL, "rtol": RTOL, "reference": "independent_cpu_fp32",
          "automatic_release_allowed": False, "performance_conclusion": "not_measured"})
    try:
        if sys.version_info[:2] != (3, 10):
            raise ValueError("requires locked Python3.10")
        if importlib.metadata.version("sglang-kernel") != PACKAGE_VERSION:
            raise ValueError("kernel distribution version drift")
        if BINARY.resolve(strict=True) != BINARY or sha256_file(BINARY) != BINARY_SHA256:
            raise ValueError("kernel binary identity drift")
        import torch

        if torch.cuda.device_count() != 1:
            raise ValueError("requires exactly one visible device")
        device = torch.cuda.get_device_properties(0)
        if ((device.pci_domain_id, device.pci_bus_id, device.pci_device_id) != (0, 177, 0)
                or device.gcnArchName.split(":")[0] != "gfx936"):
            raise ValueError("physical device identity mismatch")
        torch.cuda.set_device(0)
        torch.cuda.set_per_process_memory_fraction(1 / 32, 0)
        import sgl_kernel

        loaded_path = Path(sgl_kernel.common_ops.__file__).resolve(strict=True)
        if loaded_path != BINARY or sha256_file(loaded_path) != BINARY_SHA256:
            raise ValueError("actually loaded common_ops identity mismatch")
        emit({"event": "identity", "loaded_binary": str(loaded_path),
              "binary_sha256": BINARY_SHA256, "torch": torch.__version__,
              "hip": torch.version.hip, "arch": device.gcnArchName,
              "wrapper_module": sgl_kernel.rotary_embedding.__module__,
              "python": sys.version, "pci": "0000:b1:00.0"})
        inv_freq = 1.0 / (1000000.0 ** (torch.arange(0, 64, 2, dtype=torch.float32) / 64))
        phase = torch.outer(torch.arange(32768, dtype=torch.float32), inv_freq)
        cache_cpu = torch.cat((phase.cos(), phase.sin()), dim=-1).to(torch.bfloat16)
        cache = cache_cpu.to("cuda:0")
        with torch.inference_mode():
            for tokens in TOKENS:
                for seed in SEEDS:
                    for padded in (False, True):
                        if not run_case(torch, sgl_kernel.rotary_embedding, cache, cache_cpu,
                                        tokens=tokens, seed=seed, padded=padded):
                            emit({"event": "summary", "status": "failed",
                                  "automatic_release_allowed": False})
                            return 1
        emit({"event": "summary", "status": "bounded_cases_passed", "case_count": 48,
              "model_path_verified": False, "launch_bound_issue_resolved": False,
              "framework_pair_accepted": False, "automatic_release_allowed": False})
        return 0
    except Exception as exc:
        emit({"event": "summary", "status": "failed", "error_type": type(exc).__name__,
              "error": str(exc)[:1500], "automatic_release_allowed": False})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
