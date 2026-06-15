# Sandbox container for archie shell tool execution.
# This is a disposable environment — created per session, destroyed after.
# Contains all the CLI tools a developer might need without touching the host.

FROM debian:bookworm-slim

# Build args for matching host user identity (avoids file permission issues)
ARG USERNAME=archie
ARG USER_UID=1000
ARG TARGETARCH

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# --- System packages ---
# Core utilities and developer tools available in Debian repos
# fd-find is packaged as "fd-find" in Debian but the binary is "fdfind".
# Create a symlink so "fd" works as expected.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    openssh-client \
    gnupg \
    curl \
    dnsutils \
    fd-find \
    git \
    iputils-ping \
    nano \
    netcat-openbsd \
    pandoc \
    ripgrep \
    shellcheck \
    sqlite3 \
    sudo \
    tree \
    unzip \
    wget \
    zip \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd

# --- uv (Python package manager + Python installer) ---
# uv is used both to install Python and manage packages inside the sandbox
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN UV_PYTHON_INSTALL_DIR=/usr/local uv python install && \
    PYTHON_PATH=$(find /usr/local -name "cpython-*" -type d | head -1) && \
    ln -s "$PYTHON_PATH/bin/python3" /usr/local/bin/python3 && \
    ln -s /usr/local/bin/python3 /usr/local/bin/python

# Install jq
RUN curl -L "https://github.com/jqlang/jq/releases/latest/download/jq-linux-${TARGETARCH}" \
        -o /usr/local/bin/jq && chmod +x /usr/local/bin/jq

# --- yq (YAML processor, like jq for YAML) ---
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${TARGETARCH}" \
    -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq

# --- AWS CLI v2 ---
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip; \
    else \
        curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscli.zip; \
    fi && \
    unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscli.zip

# --- OpenTofu (Terraform-compatible IaC tool) ---
RUN curl -fsSL https://get.opentofu.org/install-opentofu.sh | bash -s -- --install-method standalone

# --- terraform-docs (generates docs from Terraform modules) ---
RUN TFDOCS_VERSION=$(curl -fsSL https://api.github.com/repos/terraform-docs/terraform-docs/releases/latest | jq -r .tag_name) && \
    curl -fsSL "https://github.com/terraform-docs/terraform-docs/releases/download/${TFDOCS_VERSION}/terraform-docs-${TFDOCS_VERSION}-linux-${TARGETARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin terraform-docs && chmod +x /usr/local/bin/terraform-docs

# --- GitHub CLI ---
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && rm -rf /var/lib/apt/lists/*

# --- difftastic (structural diff tool) ---
RUN DIFFT_VERSION=$(curl -s https://api.github.com/repos/Wilfred/difftastic/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && \
    case ${TARGETARCH} in \
        amd64) DIFFT_ARCH="x86_64" ;; \
        arm64) DIFFT_ARCH="aarch64" ;; \
    esac && \
    curl -L "https://github.com/Wilfred/difftastic/releases/download/${DIFFT_VERSION}/difft-${DIFFT_ARCH}-unknown-linux-gnu.tar.gz" \
        -o /tmp/difft.tar.gz && \
    tar -xzf /tmp/difft.tar.gz -C /usr/local/bin && \
    chmod +x /usr/local/bin/difft && \
    rm /tmp/difft.tar.gz

# --- xh (modern HTTP client, like httpie but faster) ---
RUN XH_VERSION=$(curl -s https://api.github.com/repos/ducaale/xh/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && \
    case ${TARGETARCH} in \
        amd64) XH_ARCH="x86_64" ;; \
        arm64) XH_ARCH="aarch64" ;; \
    esac && \
    curl -L "https://github.com/ducaale/xh/releases/download/${XH_VERSION}/xh-${XH_VERSION}-${XH_ARCH}-unknown-linux-musl.tar.gz" \
        -o /tmp/xh.tar.gz && \
    tar -xzf /tmp/xh.tar.gz -C /tmp && \
    mv /tmp/xh-${XH_VERSION}-${XH_ARCH}-unknown-linux-musl/xh /usr/local/bin/ && \
    chmod +x /usr/local/bin/xh && \
    rm -rf /tmp/xh*

# --- just (command runner, like make but simpler) ---
RUN JUST_VERSION=$(curl -s https://api.github.com/repos/casey/just/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && \
    case ${TARGETARCH} in \
        amd64) JUST_ARCH="x86_64" ;; \
        arm64) JUST_ARCH="aarch64" ;; \
    esac && \
    curl -L "https://github.com/casey/just/releases/download/${JUST_VERSION}/just-${JUST_VERSION}-${JUST_ARCH}-unknown-linux-musl.tar.gz" \
        -o /tmp/just.tar.gz && \
    tar -xzf /tmp/just.tar.gz -C /usr/local/bin just && \
    chmod +x /usr/local/bin/just && \
    rm /tmp/just.tar.gz

# --- shfmt (shell script formatter) ---
RUN SHFMT_VERSION=$(curl -s https://api.github.com/repos/mvdan/sh/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && \
    case ${TARGETARCH} in \
        amd64) SHFMT_ARCH="amd64" ;; \
        arm64) SHFMT_ARCH="arm64" ;; \
    esac && \
    curl -L "https://github.com/mvdan/sh/releases/download/${SHFMT_VERSION}/shfmt_${SHFMT_VERSION}_linux_${SHFMT_ARCH}" \
        -o /usr/local/bin/shfmt && \
    chmod +x /usr/local/bin/shfmt

# --- Pre-seed GitHub SSH host keys (avoids interactive prompts on first git clone) ---
RUN mkdir -p /etc/ssh && ssh-keyscan github.com >> /etc/ssh/ssh_known_hosts 2>/dev/null

# --- RTK (Rust Token Killer) — compresses shell output for LLM consumption ---
# Reduces token usage by 60-90% on common dev commands (git, test runners, linters, etc.)
# The shell tool routes commands through rtk for compact output.
RUN curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh && \
    mv ~/.local/bin/rtk /usr/local/bin/rtk

# --- Create non-root user matching host UID ---
# This ensures files created inside the container have correct ownership
# when viewed from the host (via the mounted project directory).
RUN groupadd -g ${USER_UID} ${USERNAME} && \
    useradd -m -u ${USER_UID} -g ${USER_UID} -s /bin/bash ${USERNAME} && \
    echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# --- Create project mount point ---
RUN mkdir /workspace
WORKDIR /workspace

USER ${USERNAME}

# Default command — keep container alive for exec calls
CMD ["sleep", "infinity"]
