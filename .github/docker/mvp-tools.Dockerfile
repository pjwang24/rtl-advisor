FROM ghcr.io/astral-sh/uv:0.11.5@sha256:bd44bb8253b99699d744ccc3db5f4d10c39a71ddbe97cf5c0361f65bf51a33f9 AS uv

FROM python:3.13-slim-bookworm@sha256:dd86541a59b252667f4c12f8b2ee17216de37dd65ac773bf097bef996fa78860

LABEL org.opencontainers.image.source="https://github.com/pjwang24/rtl-advisor"
LABEL org.opencontainers.image.description="Pinned RTL Advisor formal and synthesis integration environment"

COPY --from=uv /uv /uvx /bin/

# Official YosysHQ OSS CAD Suite release 2026-03-06, linux-x64 asset. This is
# the first available Linux bundle after the Yosys 0.63 release.
ADD --checksum=sha256:4b514b77fc85a2587fbb2784bffc18279a93f7b0fd8dd1d162f6991b10a9cbe8 \
    https://github.com/YosysHQ/oss-cad-suite-build/releases/download/2026-03-06/oss-cad-suite-linux-x64-20260306.tgz \
    /tmp/oss-cad-suite.tgz
RUN python -c 'from pathlib import Path; import tarfile; archive=Path("/tmp/oss-cad-suite.tgz"); destination=Path("/opt/oss-cad-suite"); destination.mkdir(parents=True); bundle=tarfile.open(archive, "r:gz"); members=bundle.getmembers(); roots={Path(member.name).parts[0] for member in members if Path(member.name).parts}; assert len(roots) == 1, roots; bundle.extractall(destination, members=members, filter="data"); bundle.close(); root=destination / roots.pop(); [child.rename(destination / child.name) for child in tuple(root.iterdir())]; root.rmdir(); archive.unlink()'

# The suite's Perl launcher is unnecessary for lint-only use and depends on
# host Perl modules that are intentionally absent from the slim base image.
RUN ln -sf /opt/oss-cad-suite/bin/verilator_bin /opt/oss-cad-suite/bin/verilator

ENV PATH="/opt/oss-cad-suite/bin:/workspace/.venv/bin:${PATH}"
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
WORKDIR /workspace

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY tests ./tests
COPY examples ./examples
COPY rtl-advisor.toml ./
COPY .github/scripts/run-mvp-tool-smoke.sh ./.github/scripts/run-mvp-tool-smoke.sh

# The library is kept out of the wheel and fetched by immutable URL plus digest.
ADD --checksum=sha256:8d540a4d4cf6d09d27c87ad067857a9c0c2eeb023ab7a56e058cd3113db4e9b1 \
    https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/036d106273e66855cd5214d49518fd0f0df7de61/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib \
    /workspace/third_party/nangate45/NangateOpenCellLibrary_typical.lib

RUN uv sync --frozen --extra v2 --group dev

CMD ["sh", ".github/scripts/run-mvp-tool-smoke.sh"]
