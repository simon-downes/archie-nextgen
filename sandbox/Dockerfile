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
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    dnsutils \
    git \
    iputils-ping \
    jq \
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
    && rm -rf /var/lib/apt/lists/*

# fd-find is packaged as "fd-find" in Debian but the binary is "fdfind".
# Create a symlink so "fd" works as expected.
RUN apt-get update && apt-get install -y --no-install-recommends fd-find \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

# --- uv (Python package manager + Python installer) ---
# uv is used both to install Python and manage packages inside the sandbox
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN uv python install 3.14

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
RUN DFT_VERSION=$(curl -fsSL https://api.github.com/repos/Wilfred/difftastic/releases/latest | jq -r .tag_name) && \
    curl -fsSL "https://github.com/Wilfred/difftastic/releases/download/${DFT_VERSION}/difft-${TARGETARCH}-unknown-linux-gnu.tar.gz" \
    | tar -xz -C /usr/local/bin && chmod +x /usr/local/bin/difft

# --- xh (modern HTTP client, like httpie but faster) ---
RUN XH_VERSION=$(curl -fsSL https://api.github.com/repos/ducaale/xh/releases/latest | jq -r .tag_name | sed 's/^v//') && \
    curl -fsSL "https://github.com/ducaale/xh/releases/download/v${XH_VERSION}/xh-v${XH_VERSION}-${TARGETARCH}-unknown-linux-musl.tar.gz" \
    | tar -xz --strip-components=1 -C /usr/local/bin "xh-v${XH_VERSION}-${TARGETARCH}-unknown-linux-musl/xh" && \
    chmod +x /usr/local/bin/xh

# --- just (command runner, like make but simpler) ---
RUN JUST_VERSION=$(curl -fsSL https://api.github.com/repos/casey/just/releases/latest | jq -r .tag_name) && \
    curl -fsSL "https://github.com/casey/just/releases/download/${JUST_VERSION}/just-${JUST_VERSION}-${TARGETARCH}-unknown-linux-musl.tar.gz" \
    | tar -xz -C /usr/local/bin just && chmod +x /usr/local/bin/just

# --- shfmt (shell script formatter) ---
RUN SHFMT_VERSION=$(curl -fsSL https://api.github.com/repos/mvdan/sh/releases/latest | jq -r .tag_name) && \
    curl -fsSL "https://github.com/mvdan/sh/releases/download/${SHFMT_VERSION}/shfmt_${SHFMT_VERSION}_linux_${TARGETARCH}" \
    -o /usr/local/bin/shfmt && chmod +x /usr/local/bin/shfmt

# --- Pre-seed GitHub SSH host keys (avoids interactive prompts on first git clone) ---
RUN mkdir -p /etc/ssh && ssh-keyscan github.com >> /etc/ssh/ssh_known_hosts 2>/dev/null

# --- Create non-root user matching host UID ---
# This ensures files created inside the container have correct ownership
# when viewed from the host (via the mounted project directory).
RUN groupadd -g ${USER_UID} ${USERNAME} && \
    useradd -m -u ${USER_UID} -g ${USER_UID} -s /bin/bash ${USERNAME} && \
    echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER ${USERNAME}

# Default command — keep container alive for exec calls
CMD ["sleep", "infinity"]
