# BW20 replacement-hotspot triage: paged KV-cache write

Status: **PROVISIONAL hotspot selection**, not a Candidate or performance verdict.

## Evidence and fixed scope

- Source: BW20 Formal Stage 0 v4 Run `dc883c86-9aa8-5550-a8ea-0094a9866519`, profiler Job at fencing token 40. Raw one-rank prefill trace: `/home/github/hcu-auto-opt-runtime/bw20-stage0/ba2d69ae-387a-44a4-9c9f-ee07a020c2be/formal-evidence/bw20-c-jobs/ec54ac29-cbd8-5d2a-b6c3-ec15ae11fa17/40/profiler/prefill.trace.json.gz`, SHA256 `6e4a11a65d0595a1a1ce939d7683f92eb01e91b989acd832acb686796aed1364`.
- The trace comes from SGLang `bench_one_batch`, Qwen2.5-0.5B-Instruct, TP=1, batch=1, input 128, output 8; it captures one prefill after one warmup. It is not the old 4,090-token allocator M1 workload, not a statistically repeated BW20 profiler campaign, and not a service-level baseline.
- Locked SGLang baseline Commit `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`, Baseline Source Hash `sha256:215e9259a7afd87e93924487a8153e5e80f6f831e5a0056dce5350e031363c0b`. The current Target Snapshot and image must be re-read and bound to any new Task; no old Task/Run ID is transferable.
- Analysis: `profile-llm-torch` single-trace report under local ignored `results/bw20-new-hotspot-20260924/`. The optional model-architecture diagram helper is unavailable and the report marks it explicitly. The parser's “Fused MoE” pattern is not treated as a Qwen2.5 hotspot conclusion.
- The CPU-only reference module initially uses representative locations of the observed 128-token *shape*; the trace does not reveal the actual location values. These representative cases are development fixtures, not a frozen business workload. The fusion-operator template validator assumes `high_level_operator_library` integration and therefore rejects this project's intentional `startup Overlay` integration label; we retain the truthful label and use the repository's M1 contracts as authority.

## Why this site, and why the others were not chosen

`python/sglang/srt/mem_cache/memory_pool.py:1110` `set_kv_buffer` is the selected **hypothesis**. The trace attributes approximately 0.54 ms (13.2% of summed GPU kernel time) to index writes at this site and another approximately 0.20 ms (4.9%) to vectorized elementwise operations mostly attributed there. Raw CPU events show 48 `aten::_index_put_impl_` calls with BF16 cache shape `[72279,2,64,64]`, input `[128,2,64]`, input strides `[1152,64,1]`; this matches 24 layers writing K and V separately in the `SGLANG_KV_LAYOUT_DCU_FA` path. The source performs `loc // 64`, `loc % 64`, then separate advanced-index K and V stores. A single carefully guarded Triton scatter could plausibly combine address calculation and both writes while preserving the page layout.

The approximately 0.74 ms combined kernel time is at most about 18% of the 4.11 ms GPU busy union in this one trace. The 27.11 ms profiler wall window contains about 84.8% gaps, so these fractions are **not** an endpoint speedup prediction. The GEMM rows are larger but route to library `F.linear`; FlashAttention, RMSNorm, SiLU and RoPE already invoke fused/vendor kernels or require a non-Overlay change. `compute_position_triton` is already fused and much smaller. We therefore prefer an editable Python/Triton KV-write site, while keeping all gain estimates provisional.

## One bounded development hypothesis

Read-only source check on BW20 confirmed that `/home/github/hcu-auto-opt-runtime/sglang-das`
is clean at `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`. In that exact tree,
`memory_pool.py:set_kv_buffer` first converts/scales dtype when needed, then uses
`loc // 64` and `loc % 64` for the DCU FA layout. Capture mode with `alt_stream`
has a separate K/V stream-ordering branch. Source inspection alone does **not**
prove the observed `loc` values are unique or that the proposed fused path is
safe under capture; those remain explicit gates before Candidate intake.
The same baseline's paged allocator `alloc_extend` asserts `len(torch.unique(out_indices))
== len(out_indices)` only under `SGLANG_DEBUG_MEMORY_POOL`. That is useful design
intent, **not** a production-time guarantee; the measured input family still
needs a recorded distinctness check, and any broader service path must retain
the original implementation unless its invariant is established separately.

For the observed BF16 paged layout and a provably distinct `loc` vector, fuse `page=loc//64`, `offset=loc%64`, and K/V cache stores in one startup-Overlay Triton implementation. Dispatch may use only host-known metadata (shape, dtype, stride, layout and capture/stream state); it must not use tensor-to-Python booleans, implicit device synchronization, or a shape check as a substitute for proving index uniqueness. Unsupported layouts, dtype conversion/scaling, capture/alternate-stream behavior, overlap, duplicate or invalid locations must retain the original path unless an independent oracle covers them. Do not mutate the locked Baseline or an existing Run.

Correctness must compare the entire K and V cache tensors and untouched positions, not only a sorted set of indices. Include token counts 0/1/63/64/65/128, page crossings, permuted valid locations, non-contiguous K/V input strides, capture mode, dtype/storage conversion and stream ordering as applicable. The measured target shape must be frozen from an actual matching BW20 run; the Stage 0 trace alone does not establish all runtime invariants.

## Integration gate before a new Agent Run

The current BW20 M1 source package whitelist, Overlay mount, correctness producer, Docker performance module and workload factory are hardcoded to `PagedTokenToKVPoolAllocator.free`. Repointing only the Agent prompt would produce a Proposal that the formal Build/HCU path cannot validate. Add a separate, explicitly registered Real KV-cache M1 profile or a tested workload dispatch with the same fail-closed bindings; freeze a new correctness spec and matching measurement workload first. Only then create a new Task/Hotspot/Generation Run with fresh identities and a bounded model budget. Build → correctness → B Harness → D adjudication → human signoff remains unchanged; `automatic_release_allowed=false`.
