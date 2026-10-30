# TrustPCB RF worker image: openEMS + CSXCAD built from source at a pinned tag.
#
#   docker build -t trustpcb-ems:dev -f docker/ems/Dockerfile .
#
# Pinned: thliebig/openEMS-Project v0.37.0 (commit 974294ba7d812ef5a46a4760593057aee7ce6866,
# 2026-10-09). Submodules are taken at the commits that tag records.
# Builds natively on linux/arm64 and linux/amd64. No GUI (no Qt/VTK).

ARG OPENEMS_REF=v0.37.0
ARG OPENEMS_COMMIT=974294ba7d812ef5a46a4760593057aee7ce6866

# ---------------------------------------------------------------- builder
FROM debian:bookworm-slim AS builder
ARG OPENEMS_REF
ARG OPENEMS_COMMIT
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git cmake ca-certificates \
      libhdf5-dev libtinyxml-dev libboost-all-dev zlib1g-dev libcgal-dev \
      python3-dev python3-pip python3-setuptools python3-setuptools-scm python3-wheel \
      cython3 python3-numpy python3-h5py python3-scipy \
    && rm -rf /var/lib/apt/lists/*

# CSXCAD links VTK (IOGeometry, IOPLY) for geometry export even without the GUI.
RUN apt-get update && apt-get install -y --no-install-recommends libvtk9-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Full (non-shallow) clone: the project derives version strings from git describe.
RUN git clone https://github.com/thliebig/openEMS-Project.git openEMS-Project \
 && cd openEMS-Project \
 && git checkout "${OPENEMS_COMMIT}" \
 && git describe --tags --exact-match | grep -x "${OPENEMS_REF}" \
 && git submodule update --init fparser CSXCAD openEMS

ENV PREFIX=/opt/ems
RUN mkdir /build && cd /build \
 && cmake /src/openEMS-Project -DBUILD_APPCSXCAD=NO -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=${PREFIX} \
 && make -j4

# Python bindings (Cython), against the apt numpy that the runtime also uses.
ENV CSXCAD_INSTALL_PATH=${PREFIX} OPENEMS_INSTALL_PATH=${PREFIX} \
    CSXCAD_NOSCM=1 OPENEMS_NOSCM=1 \
    LD_LIBRARY_PATH=${PREFIX}/lib
RUN cd /src/openEMS-Project/CSXCAD/python \
 && pip3 install --no-build-isolation --no-deps --break-system-packages --prefix ${PREFIX}/py . \
 && cd /src/openEMS-Project/openEMS/python \
 && SP=$(dirname $(find ${PREFIX}/py -type d -name CSXCAD -path '*-packages*' | head -1)) \
 && echo "CSXCAD installed in $SP" && ls $SP/CSXCAD \
 && PYTHONPATH=$SP \
    pip3 install --no-build-isolation --no-deps --break-system-packages --prefix ${PREFIX}/py .

# Record the exact runtime shared-library packages the binaries need.
RUN set -e; \
    find ${PREFIX} -type f \( -name '*.so*' -o -path '*/bin/*' \) | xargs ldd 2>/dev/null \
      | awk '/=>/ {print $3}' | grep '^/' | grep -v "^${PREFIX}" | sort -u > /libs.txt; \
    : > /runtime-pkgs.txt; \
    while read -r l; do \
      dpkg -S "$(readlink -f "$l")" 2>/dev/null | head -1 | cut -d: -f1 >> /runtime-pkgs.txt; \
    done < /libs.txt; \
    sort -u -o /runtime-pkgs.txt /runtime-pkgs.txt; \
    echo "$(wc -l < /libs.txt) libs -> $(wc -l < /runtime-pkgs.txt) packages"; cat /runtime-pkgs.txt
RUN find ${PREFIX} -name '*.a' -delete \
 && rm -rf ${PREFIX}/include ${PREFIX}/share/doc \
 && (find ${PREFIX} -type f \( -name '*.so*' -o -path '*/bin/*' \) -exec strip --strip-unneeded {} + 2>/dev/null || true)

# ---------------------------------------------------------------- runtime
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
COPY --from=builder /runtime-pkgs.txt /runtime-pkgs.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-numpy python3-h5py python3-scipy libgomp1 \
      $(grep -v -E '^(libc6|libgcc-s1|libstdc\+\+6)$' /runtime-pkgs.txt | tr '\n' ' ') \
    && rm -rf /var/lib/apt/lists/* \
# Trim what only came in as packaging dependencies: scipy pulls in pythran with a
# full C++ toolchain and -dev headers, VTK pulls a software GL stack. None of it is
# used at run time (nothing renders), so drop it without touching what is linked.
    && dpkg --purge --force-depends \
         $(dpkg-query -W -f='${Package}\n' | grep -E -- '-dev$|^(gcc|g\+\+|cpp)(-12)?$|^binutils|^python3-(pythran|gast|beniget|ply)$|^libgl1-mesa-dri$|^libllvm15$|^libz3-4$|^proj-data$|^libasan8$|^libtsan2$|^libubsan1$|^libhwasan0$|^libitm1$|^liblsan0$|^libatomic1$|^libgprofng0$|^libctf|^libbfd|^libopcodes|^libsframe') \
    && rm -rf /usr/share/doc /usr/share/man /usr/share/locale /usr/share/info /var/cache/* \
    && find /usr/lib/python3 /usr/lib/python3.11 -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
COPY --from=builder /opt/ems /opt/ems
ENV PATH=/opt/ems/bin:$PATH \
    LD_LIBRARY_PATH=/opt/ems/lib \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=4
# site-packages dir is version specific; expose it via a .pth file
RUN echo "/opt/ems/lib" > /etc/ld.so.conf.d/ems.conf && ldconfig \
 && ! ldd /opt/ems/bin/openEMS | grep "not found" \
 && d=$(dirname $(find /opt/ems/py -type d -name CSXCAD -path '*-packages*' | head -1)) \
 && echo "$d" > /usr/lib/python3/dist-packages/ems.pth \
 && python3 -c "import CSXCAD, openEMS, numpy, h5py, scipy.interpolate, scipy.ndimage; print('bindings ok', openEMS.__file__)"
WORKDIR /app
COPY worker/ /app/
CMD ["python3", "/app/run_scenario.py", "/job"]
