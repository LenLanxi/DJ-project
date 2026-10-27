# Official SDK archive is deliberately kept in ignored Build/RTCNative.
# Build context: modules/rtc. This image does not open audio/video devices.
FROM debian:trixie-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends g++ nlohmann-json3-dev ca-certificates libatomic1 && rm -rf /var/lib/apt/lists/*
COPY Build/RTCNative/VolcEngineRTC_Linux_3.60.107.700_x86_64_Release /sdk
COPY NativeProbe.cpp /src/NativeProbe.cpp
RUN g++ -std=c++17 -O2 -pthread -Wall -Wextra -Wno-unused-parameter -I/sdk/include /src/NativeProbe.cpp -L/sdk/lib -Wl,-rpath,/sdk/lib -lVolcEngineRTC -o /NativeProbe

FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libatomic1 libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /sdk/lib /sdk/lib
COPY --from=builder /NativeProbe /usr/local/bin/NativeProbe
ENV LD_LIBRARY_PATH=/sdk/lib
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/NativeProbe"]
