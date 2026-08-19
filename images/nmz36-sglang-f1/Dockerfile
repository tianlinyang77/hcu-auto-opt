ARG BASE_IMAGE=10.16.1.152:5000/jenkins/model_test_env/sglang@sha256:ee8eb5a76e9a4060ef2ffcbb9fa0da09aed2132c35592d1e723454a770dd38db
FROM ${BASE_IMAGE}

ARG SGLANG_SOURCE_COMMIT=dad582f28458cd0e11e0be675fbe7fcc7ab65ac1
ARG SGLANG_PATCH_SHA256=889148e8cbd12969397040c76a46a97cc9d557cbec3c65f792676cbfec9a4133
ARG SGLANG_WHEEL_FILE=sglang-0.5.12+das.opt1.dtk2604.torch2100.2606021957.gdad582.hcuopt1-cp310-cp310-linux_x86_64.whl
ARG SGLANG_WHEEL_SHA256=20f3fef4bf87e9afd44d71ffabf4b26ec34934e68a8fad934d06d3c8634949aa
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

LABEL org.opencontainers.image.title="HCU Auto Opt F1 SGLang runtime"
LABEL org.opencontainers.image.description="Wheel-built SGLang 0.5.12 runtime for the nmz36 F1 Framework Smoke"
LABEL org.opencontainers.image.base.digest="sha256:ee8eb5a76e9a4060ef2ffcbb9fa0da09aed2132c35592d1e723454a770dd38db"
LABEL org.opencontainers.image.revision="${SGLANG_SOURCE_COMMIT}"
LABEL io.hcuopt.target="nmz36-sglang-0.5.12"
LABEL io.hcuopt.sglang.patch.sha256="${SGLANG_PATCH_SHA256}"
LABEL io.hcuopt.sglang.wheel.sha256="${SGLANG_WHEEL_SHA256}"
LABEL io.hcuopt.fix.kernels="0.12.3"
LABEL io.hcuopt.fix.apache-tvm-ffi="0.1.9"

COPY ${SGLANG_WHEEL_FILE} /opt/hcuopt-image/${SGLANG_WHEEL_FILE}

RUN set -eux; \
    printf '%s  %s\n' \
      "${SGLANG_WHEEL_SHA256}" \
      "/opt/hcuopt-image/${SGLANG_WHEEL_FILE}" \
      | sha256sum -c -; \
    python -m pip install --index-url "${PIP_INDEX_URL}" \
      --no-cache-dir --no-deps --upgrade \
      'kernels==0.12.3' \
      'apache-tvm-ffi==0.1.9'; \
    python -m pip install --no-cache-dir --no-deps --force-reinstall \
      "/opt/hcuopt-image/${SGLANG_WHEEL_FILE}"; \
    python -m py_compile \
      /usr/local/lib/python3.10/dist-packages/sglang/srt/models/qwen2.py; \
    python -c "import importlib.metadata as m; assert m.version('sglang').endswith('hcuopt1'); assert m.version('kernels') == '0.12.3'; assert m.version('apache-tvm-ffi') == '0.1.9'"; \
    grep -F \
      'from sglang.srt.utils.hf_transformers_utils import get_rope_config' \
      /usr/local/lib/python3.10/dist-packages/sglang/srt/models/qwen2.py
