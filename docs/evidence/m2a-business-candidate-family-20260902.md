# M2a 双候选源码集合准备与独立验证记录

## 结论

C 线已经为同一个 SGLang 内存分配器待优化位置准备两份真实、内容不同的 Python 候选源码，
并使用现有 `CandidateSourcePackageManifest`、`CandidateSourcePackageRef`、
`BusinessCandidateFamilyManifest` 和 `BusinessCandidateFamilyVerifier` 完成可信存储重读及
Baseline 源码重放验证。C 线结论为 `accepted_for_formal_window`。

本次仅准备和验证后续正式测试要使用的源码，不访问 HCU、不运行 Formal Round、不测量性能，
也不声称任何候选更快。两个候选仍必须在后续正式流程中依次通过构建、正确性和性能验证。

## 共同绑定

- SGLang Baseline Commit：`dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`；
- Baseline Source Hash：
  `sha256:215e9259a7afd87e93924487a8153e5e80f6f831e5a0056dce5350e031363c0b`；
- Target Snapshot：`ffb9f94e-2035-5ff6-9b06-f1b5fb163196`；
- Formal Stage0Run：`dd50c381-75dd-5a64-9211-640c602dc817`；
- Baseline Epoch：`1ab0480c-6f16-544c-a835-655599eaea6c`；
- Hotspot：`ec941d47-206a-5f18-8538-ae9c18c1e0ec`；
- 待优化位置：
  `sglang.srt.mem_cache.allocator.PagedTokenToKVPoolAllocator.free`；
- 替换文件：`python/sglang/srt/mem_cache/allocator.py`；
- 测试任务：`m1-qwen2.5-0.5b-prefill-4090-1-c1`。

## 两个候选修改

原始实现使用：

```python
torch.unique(free_index // self.page_size)
```

候选一沿用 M1 已验收的真实修改，在已经确认页索引连续的路径中使用：

```python
torch.unique_consecutive(free_index // self.page_size)
```

- Candidate ID：`de4e9427-340b-5428-bfa7-49aa52a1acae`；
- Candidate Source Hash：
  `sha256:f27c1546bc5bd46741ae98ad0b96d51974a0108af316f9d557daa2f82556fbc2`；
- 文件 Hash：
  `sha256:93bd2a5aec5a01cb61ac8c6f2ddca7bd25d63ffe4aec8fed7f60199c3581da8a`。

候选二针对同一个完整分页释放路径，直接抽取每一页的首个索引，避免执行全局去重：

```python
free_index[:: self.page_size] // self.page_size
```

- Candidate ID：`fd55580b-1e03-5907-bad8-493ceacd1533`；
- Candidate Source Hash：
  `sha256:a830e81d589f2199a223fbbb4afa9d74bfcbf546540259e22f32ffa02b83162a`；
- 文件 Hash：
  `sha256:136719de7f9d8ea6bd9798c9d85b0e4126e6fd31a53ae4172516c239b94cd5d9`。

候选二是真正会改变 SGLang 执行代码的优化假设，不是修改注释、复制候选一或只更换 Candidate
ID。它依赖该分配器“释放输入由完整、连续的页块组成”的约束；后续正式正确性验证若不能证明
该约束，候选二必须被拒绝，不能进入性能结论。

## 固定源码包和候选集合

- Candidate Package Store 描述：
  `config/m2/nmz36-business-candidate-store-v1.json`；
- Store Hash：
  `sha256:2d55a8825dfac73bb2248c73547fc5dbc2b16fd1baaf8b635ef2760e40db04b1`；
- 双候选集合：`config/m2/nmz36-business-candidate-family-v1.json`；
- `source_family_hash`：
  `sha256:a9f03a6b0a87bf1c80aa29b9eb16e04da28e759ca881af0fa12de456f15a57c1`。

两个源码包都放在同一个内容寻址目录中。每个包继续使用既有
`m1-candidate-source-v1` 格式；双候选集合继续使用既有
`m2a-business-candidate-family-v1` 格式，没有增加第二套 Candidate、Artifact 或 Family 格式。

## 独立验证

独立验证程序执行了以下检查：

1. 重新读取 Store 描述、双候选集合和两个源码包，并要求 JSON 为规范格式；
2. 重新计算文件、Manifest、Package、Store 和 `source_family_hash`；
3. 拒绝不同 ID 但源码内容相同的候选；
4. 从锁定 Baseline 源码分别应用两个替换文件，重新计算完整源码树 Hash；
5. 要求重放得到的两个 Candidate Source Hash 与源码包记录完全一致；
6. 固定 `synthetic=false`、`hcu_accessed=false`、`formal_round_created=false`、
   `performance_conclusion=not_measured` 和 `automatic_release_allowed=false`。

机器可读记录：
`docs/evidence/m2a-business-candidate-family/sha256-bc70a9afd2fc8e90ca7be7fc293675ebe71bf94e15d07b99d6f94567a0e5952c.json`。

记录 Hash：
`sha256:bc70a9afd2fc8e90ca7be7fc293675ebe71bf94e15d07b99d6f94567a0e5952c`。
