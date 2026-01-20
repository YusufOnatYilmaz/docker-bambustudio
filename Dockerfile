# syntax=docker/dockerfile:1
#
# Headless BambuStudio Slicer API
# Ubuntu 24.04 + BambuStudio CLI + Node.js API Server
#
# Build: docker build -t bambu-slicer-api .
# Run:   docker run -p 8080:8080 -v bambu_config:/config bambu-slicer-api
#

FROM ubuntu:24.04

ARG BAMBUSTUDIO_VERSION
ARG DEBIAN_FRONTEND=noninteractive

LABEL maintainer="3d-hub"
LABEL description="Headless BambuStudio Slicer API for automated G-code generation"

# ============================================================
# 1. System packages & dependencies
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Basic utilities
    ca-certificates \
    curl \
    wget \
    gnupg \
    locales \
    tzdata \
    # Python
    python3 \
    python3-pip \
    # Build tools (for some npm packages)
    build-essential \
    # X11/Display (required by BambuStudio even in CLI mode)
    xvfb \
    x11-utils \
    x11-xserver-utils \
    xauth \
    # OpenGL/Mesa for software rendering (Ubuntu 24.04 package names)
    libgl1 \
    libglx-mesa0 \
    libgl1-mesa-dri \
    libegl1 \
    libegl-mesa0 \
    libgles2 \
    libosmesa6 \
    mesa-utils \
    # GTK and GUI libraries (BambuStudio dependencies)
    libgtk-3-0 \
    libgtk-3-common \
    libglib2.0-0 \
    libgdk-pixbuf2.0-0 \
    libpango-1.0-0 \
    libcairo2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxrender1 \
    libxcursor1 \
    libxi6 \
    libxtst6 \
    libxss1 \
    libxkbcommon0 \
    libxkbcommon-x11-0 \
    # WebKit (required by BambuStudio)
    libwebkit2gtk-4.1-0 \
    # Audio (PulseAudio stubs - some apps check for it)
    libpulse0 \
    # GStreamer (media handling)
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    # Fonts
    fonts-dejavu \
    fonts-dejavu-core \
    fonts-dejavu-extra \
    fonts-liberation \
    fontconfig \
    # D-Bus
    dbus \
    dbus-x11 \
    # Misc libraries
    libfuse2 \
    libsecret-1-0 \
    libnotify4 \
    libnss3 \
    libnspr4 \
    libasound2t64 \
    libdrm2 \
    libgbm1 \
    libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# 2. Set up locale
# ============================================================
RUN locale-gen en_US.UTF-8 && \
    update-locale LANG=en_US.UTF-8
ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LANGUAGE=en_US:en

# ============================================================
# 3. Install Node.js 20 LTS
# ============================================================
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# ============================================================
# 4. Install BambuStudio from AppImage
# ============================================================
# Using V2.2.x which doesn't have the nozzle_volume_type CLI bug
# V2.3.0+ has unfixed bug causing segfault
RUN set -ex && \
    # Use V2.2.0.85 - last stable before nozzle_volume_type bug
    BAMBUSTUDIO_VERSION="v02.02.00.85" && \
    echo "Installing BambuStudio version: ${BAMBUSTUDIO_VERSION}" && \
    # Get download URL for this specific version
    DOWNLOAD_URL="https://github.com/bambulab/BambuStudio/releases/download/${BAMBUSTUDIO_VERSION}/Bambu_Studio_ubuntu-24.04_PR-7829.AppImage" && \
    echo "Download URL: ${DOWNLOAD_URL}" && \
    # Download and extract AppImage
    cd /tmp && \
    curl -L -o bambu.app "${DOWNLOAD_URL}" && \
    chmod +x bambu.app && \
    ./bambu.app --appimage-extract && \
    mv squashfs-root /opt/bambustudio && \
    # Create symlink for easier access
    ln -sf /opt/bambustudio/AppRun /usr/local/bin/bambu-studio && \
    # Cleanup
    rm -rf /tmp/*

# ============================================================
# 5. Create application directories
# ============================================================
RUN mkdir -p /app /config /config/work /config/.bambu_home /config/.xdg && \
    chmod -R 755 /app /config

# ============================================================
# 6. Copy application files
# ============================================================
WORKDIR /app

# Copy package.json first for better layer caching
COPY package*.json ./

# Install Node.js dependencies
RUN npm ci --only=production 2>/dev/null || npm install --only=production

# Copy application code
COPY server.js ./
COPY bambu_callback.py ./

# Create test files directory and copy test files
RUN mkdir -p /app/test_files
COPY tumor.stl /app/test_files/
COPY Great_Wave_bambu.3mf /app/test_files/

# Copy config files (printer, process, filament profiles)
RUN mkdir -p /config/process_config /config/printer_config /config/filament_config
COPY config/process_config/ /config/process_config/
COPY config/printer_config/ /config/printer_config/
COPY config/filament_config/ /config/filament_config/

# Make Python script executable
RUN chmod +x /app/bambu_callback.py

# ============================================================
# 7. Environment variables
# ============================================================
ENV \
    # Application
    NODE_ENV=production \
    PORT=8080 \
    CONFIG_DIR=/config \
    # OpenGL software rendering
    LIBGL_ALWAYS_SOFTWARE=1 \
    MESA_GL_VERSION_OVERRIDE=3.3 \
    MESA_GLSL_VERSION_OVERRIDE=330 \
    GALLIUM_DRIVER=llvmpipe \
    # Force X11, completely disable Wayland (critical for V2.2.x GLFW)
    XDG_SESSION_TYPE=x11 \
    WAYLAND_DISPLAY= \
    GDK_BACKEND=x11 \
    GLFW_IM_MODULE= \
    SDL_VIDEODRIVER=x11 \
    # Qt settings for headless
    QT_QPA_PLATFORM=offscreen \
    QT_OPENGL=software \
    QT_QUICK_BACKEND=software \
    # Disable GPU features
    QTWEBENGINE_DISABLE_SANDBOX=1 \
    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage --single-process --no-zygote" \
    # SSL certificates
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    # Suppress Mesa warnings
    MESA_DEBUG=silent \
    # Home directories
    HOME=/config/.bambu_home \
    XDG_RUNTIME_DIR=/config/.xdg \
    # D-Bus (prevent warnings)
    DBUS_SESSION_BUS_ADDRESS=disabled:

# ============================================================
# 8. Health check
# ============================================================
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

# ============================================================
# 9. Expose port and set volumes
# ============================================================
EXPOSE 8080
VOLUME ["/config"]

# ============================================================
# 10. Start server
# ============================================================
CMD ["node", "server.js"]
