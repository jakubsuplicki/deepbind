"""Ollama service — runtime detection, hardware profiling, model catalog & management.

Provides all backend logic for local model support:
- Hardware profiling (OS, RAM, disk, CPU, GPU detection)
- Ollama runtime detection (installed / running / version)
- Curated model catalog with hardware-based recommendations
- Model pull (proxied from Ollama API with progress streaming)
- Model selection and activation
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import ipaddress
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _sanitize_for_log(value: Any) -> str:
    """Sanitize potentially untrusted values before writing to plain-text logs."""
    return str(value).replace("\r", "").replace("\n", "")


def _normalize_and_validate_ollama_base_url(base_url: str) -> str:
    """Allow only local Ollama endpoints and return a normalized base URL.

    Security: this blocks non-loopback hosts to reduce SSRF risk when base_url
    comes from user input (query/body settings).
    """
    candidate = (base_url or DEFAULT_OLLAMA_BASE_URL).strip()
    if not candidate:
        return DEFAULT_OLLAMA_BASE_URL

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        logger.warning("Invalid Ollama base_url format: %s", _sanitize_for_log(candidate))
        return DEFAULT_OLLAMA_BASE_URL

    if parsed.scheme not in ("http", "https"):
        logger.warning("Rejected Ollama base_url with invalid scheme: %s", _sanitize_for_log(candidate))
        return DEFAULT_OLLAMA_BASE_URL

    if parsed.username or parsed.password:
        logger.warning("Rejected Ollama base_url with userinfo: %s", _sanitize_for_log(candidate))
        return DEFAULT_OLLAMA_BASE_URL

    host = (parsed.hostname or "").strip().lower()
    if not host:
        logger.warning("Rejected Ollama base_url without host: %s", _sanitize_for_log(candidate))
        return DEFAULT_OLLAMA_BASE_URL

    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"

    if not is_loopback:
        logger.warning("Rejected non-local Ollama base_url host: %s", _sanitize_for_log(host))
        return DEFAULT_OLLAMA_BASE_URL

    try:
        port = parsed.port
    except ValueError:
        logger.warning("Rejected Ollama base_url with invalid port: %s", _sanitize_for_log(candidate))
        return DEFAULT_OLLAMA_BASE_URL

    # Preserve IPv6 netloc syntax with square brackets when needed.
    if ":" in host and not host.startswith("["):
        host_for_netloc = f"[{host}]"
    else:
        host_for_netloc = host

    netloc = host_for_netloc if port is None else f"{host_for_netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", "")).rstrip("/")

# ── Default Ollama URL ───────────────────────────────────────────────────────

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"

# ── Pydantic Schemas ─────────────────────────────────────────────────────────


class HardwareProfile(BaseModel):
    os: str  # "macos" | "windows" | "linux"
    arch: str  # "arm64" | "x64"
    total_ram_gb: float
    free_disk_gb: float
    cpu_cores: int
    gpu_vendor: Optional[str] = None  # "apple" | "nvidia" | "amd" | None
    gpu_vram_gb: Optional[float] = None
    is_apple_silicon: bool = False
    tier: str = "light"  # "light" | "balanced" | "strong" | "workstation"


class RuntimeStatus(BaseModel):
    runtime: str = "ollama"
    installed: bool = False
    running: bool = False
    base_url: str = DEFAULT_OLLAMA_BASE_URL
    version: Optional[str] = None
    reachable: bool = False


class ModelCatalogEntry(BaseModel):
    id: str
    preset: str  # "fast" | "everyday" | "balanced" | "long-docs" | "reasoning" | "code" | "best-local" | "plumbing"
    ollama_model: str  # e.g. "qwen3:8b"
    litellm_model: str  # e.g. "ollama_chat/qwen3:8b"
    label: str
    download_size_gb: float
    context_window: str  # native context (e.g. "32K"). RoPE/YaRN-extended ranges are listed in strengths, not here.
    context_tokens: int
    recommended_ram_min_gb: int
    recommended_ram_max_gb: int
    min_disk_gb: float
    cpu_friendly: bool
    gpu_preferred: bool
    strengths: List[str]
    best_for: List[str]
    native_tools: bool
    internal: bool = False  # True for plumbing/classifier slots not exposed in the user-facing chat picker
    # KV-aware footprint accounting: effective footprint at request time =
    # weights + bytes_per_kv_token × ctx_len_now. These fields feed
    # `effective_footprint_bytes()` which is the predicate the future
    # memory-pressure auto-downgrade uses to ask "does this still fit?".
    # Values are approximate (architectural intuition + research-1 §"KV-cache
    # discipline"); the ratios across architectures (mamba << swa < transformer)
    # are the load-bearing signal, not the absolute numbers.
    bytes_per_kv_token: int = 4096  # transformer default; mamba/swa override below
    attention_arch: Literal["transformer", "mamba", "swa"] = "transformer"


class ModelRecommendation(BaseModel):
    model_id: str
    preset: str
    label: str
    ollama_model: str
    litellm_model: str
    download_size_gb: float
    context_window: str
    strengths: List[str]
    best_for: List[str]
    recommended_ram: str  # e.g. "16–32 GB"
    native_tools: bool
    tool_mode: str = "excluded_from_tools"  # "native_qwen3" | "adapted" | "excluded_from_tools"
    compatibility: str  # "great" | "good" | "warning" | "unsupported"
    score: int  # 0–100
    recommended: bool
    reason: str
    installed: bool
    active: bool


class PullRequest(BaseModel):
    model: str  # e.g. "qwen3:8b"
    base_url: str = DEFAULT_OLLAMA_BASE_URL


class SelectRequest(BaseModel):
    model_id: str
    litellm_model: str
    base_url: str = DEFAULT_OLLAMA_BASE_URL


class WarmUpRequest(BaseModel):
    model: str
    base_url: str = DEFAULT_OLLAMA_BASE_URL


class TestRequest(BaseModel):
    model: str
    base_url: str = DEFAULT_OLLAMA_BASE_URL


class TestResponse(BaseModel):
    success: bool
    response_text: str = ""
    latency_ms: int = 0
    tokens_per_second: float = 0.0
    tool_mode: str = ""  # "native_qwen3" | "adapted" | "excluded_from_tools"
    error: str = ""


class LoadedOllamaModel(BaseModel):
    """A model currently resident in Ollama's process memory (from /api/ps).

    Fields mirror Ollama's response shape. `size_vram` is what counts against
    the GPU/unified-memory budget; `size` is total weights + KV cache resident.
    """
    name: str
    size: int = 0
    size_vram: int = 0
    expires_at: Optional[str] = None


class RuntimeLoad(BaseModel):
    """A snapshot of how loaded the local inference runtime currently is.

    Pure-Python via psutil and Ollama's `/api/ps`. Consumed by the future
    memory-pressure auto-downgrade: when free RAM drops below the headroom
    needed for the current model + KV cache, swap to a smaller model that fits
    rather than letting Ollama OOM or thrash swap.

    On Apple Silicon, `gpu_vram_total_gb` and `gpu_vram_used_gb` are None because
    GPU memory is shared with system RAM (unified memory) — callers infer the
    available VRAM budget from `available_ram_gb` when `gpu_vendor == "apple"`.
    """
    timestamp_utc: str
    total_ram_gb: float
    available_ram_gb: float
    used_ram_gb: float
    ram_pct: float
    swap_total_gb: float
    swap_used_gb: float
    swap_pct: float
    gpu_vendor: Optional[str] = None
    gpu_vram_total_gb: Optional[float] = None
    gpu_vram_used_gb: Optional[float] = None
    loaded_models: List[LoadedOllamaModel] = []
    ollama_reachable: bool = False


# ── Model Catalog ────────────────────────────────────────────────────────────

MODEL_CATALOG: List[ModelCatalogEntry] = [
    ModelCatalogEntry(
        id="qwen3-1.7b",
        preset="fast",
        ollama_model="qwen3:1.7b",
        litellm_model="ollama_chat/qwen3:1.7b",
        label="Qwen3 1.7B",
        download_size_gb=1.4,
        context_window="32K",
        context_tokens=32768,
        recommended_ram_min_gb=8,
        recommended_ram_max_gb=16,
        min_disk_gb=4,
        cpu_friendly=True,
        gpu_preferred=False,
        strengths=["fast", "multilingual", "lightweight", "128K via YaRN"],
        best_for=["quick chat", "weak hardware", "testing"],
        native_tools=False,
        bytes_per_kv_token=2048,
        attention_arch="transformer",
    ),
    ModelCatalogEntry(
        id="qwen3-4b",
        preset="everyday",
        ollama_model="qwen3:4b",
        litellm_model="ollama_chat/qwen3:4b",
        label="Qwen3 4B",
        download_size_gb=2.5,
        context_window="32K",
        context_tokens=32768,
        recommended_ram_min_gb=12,
        recommended_ram_max_gb=24,
        min_disk_gb=6,
        cpu_friendly=True,
        gpu_preferred=False,
        strengths=["balanced", "multilingual", "128K via YaRN"],
        best_for=["everyday chat", "multilingual tasks", "moderate hardware"],
        native_tools=False,
        bytes_per_kv_token=3072,
        attention_arch="transformer",
    ),
    ModelCatalogEntry(
        id="qwen3-8b",
        preset="balanced",
        ollama_model="qwen3:8b",
        litellm_model="ollama_chat/qwen3:8b",
        label="Qwen3 8B",
        download_size_gb=5.2,
        context_window="32K",
        context_tokens=32768,
        recommended_ram_min_gb=16,
        recommended_ram_max_gb=32,
        min_disk_gb=10,
        cpu_friendly=True,
        gpu_preferred=True,
        strengths=["universal", "multilingual", "tool calling", "128K via YaRN"],
        best_for=["everyday chat", "tools", "general use"],
        native_tools=True,
        bytes_per_kv_token=4096,
        attention_arch="transformer",
    ),
    ModelCatalogEntry(
        id="ministral-3-8b",
        preset="long-docs",
        ollama_model="ministral-3:8b",
        litellm_model="ollama_chat/ministral-3:8b",
        label="Ministral 3 8B",
        download_size_gb=6.0,
        context_window="256K",
        context_tokens=262144,
        recommended_ram_min_gb=16,
        recommended_ram_max_gb=32,
        min_disk_gb=11,
        cpu_friendly=True,
        gpu_preferred=True,
        strengths=["long context", "documents", "edge deployment"],
        best_for=["long documents", "big context windows", "research"],
        native_tools=False,
        bytes_per_kv_token=3072,
        attention_arch="transformer",
    ),
    ModelCatalogEntry(
        id="gemma4-e4b",
        preset="reasoning",
        ollama_model="gemma4:e4b",
        litellm_model="ollama_chat/gemma4:e4b",
        label="Gemma 4 E4B",
        download_size_gb=9.6,
        context_window="128K",
        context_tokens=131072,
        recommended_ram_min_gb=24,
        recommended_ram_max_gb=40,
        min_disk_gb=16,
        cpu_friendly=False,
        gpu_preferred=True,
        strengths=["reasoning", "agentic", "multimodal"],
        best_for=["reasoning tasks", "agentic workflows", "analysis"],
        native_tools=True,
        bytes_per_kv_token=1024,
        attention_arch="swa",
    ),
    ModelCatalogEntry(
        id="devstral-small-2-24b",
        preset="code",
        ollama_model="devstral-small-2:24b",
        litellm_model="ollama_chat/devstral-small-2:24b",
        label="Devstral Small 2 24B",
        download_size_gb=15.0,
        context_window="256K",
        context_tokens=262144,
        recommended_ram_min_gb=32,
        recommended_ram_max_gb=64,
        min_disk_gb=22,
        cpu_friendly=False,
        gpu_preferred=True,
        strengths=["coding", "repo exploration", "multi-file edits", "384K via RoPE extension"],
        best_for=["code generation", "software engineering", "repo work"],
        native_tools=True,
        bytes_per_kv_token=5120,
        attention_arch="transformer",
    ),
    # ──────────────────────────────────────────────────────────────────────
    # Entries below carry `internal=True` because their Ollama registry tags
    # have NOT been verified against `ollama.com/library/<name>`. The user-
    # facing chat picker filters them out (`build_catalog(include_internal=False)`)
    # so a stale-tag pull doesn't 404 on the customer. Promotion to user-pickable
    # requires verifying the tag via `ollama pull <tag>` against the live registry,
    # then flipping `internal=False`.
    # ──────────────────────────────────────────────────────────────────────
    ModelCatalogEntry(
        # TODO: verify Ollama tag — was previously `gemma4:26b` with the wrong
        # "Gemma 4 27B" label (which doesn't exist). The actual variant is
        # 26B-A4B MoE per SELF-CONTAINED-APP-REVIEW.md §3; tag form on Ollama
        # is unverified.
        id="gemma4-26b-a4b",
        preset="best-local",
        ollama_model="gemma4:26b-a4b",
        litellm_model="ollama_chat/gemma4:26b-a4b",
        label="Gemma 4 26B-A4B",
        download_size_gb=15.0,
        context_window="256K",
        context_tokens=262144,
        recommended_ram_min_gb=24,
        recommended_ram_max_gb=48,
        min_disk_gb=22,
        cpu_friendly=False,
        gpu_preferred=True,
        strengths=["MoE", "reasoning", "generalist", "multimodal", "fast for size"],
        best_for=["best quality", "complex tasks", "premium local"],
        native_tools=True,
        internal=True,
        bytes_per_kv_token=1536,
        attention_arch="swa",
    ),
    ModelCatalogEntry(
        # TODO: verify Ollama tag — Qwen3 -2507 split fine-tunes use various
        # naming conventions across registries.
        id="qwen3-4b-instruct-2507",
        preset="long-docs",
        ollama_model="qwen3:4b-instruct-2507",
        litellm_model="ollama_chat/qwen3:4b-instruct-2507",
        label="Qwen3 4B Instruct 2507",
        download_size_gb=2.6,
        context_window="256K",
        context_tokens=262144,
        recommended_ram_min_gb=12,
        recommended_ram_max_gb=24,
        min_disk_gb=6,
        cpu_friendly=True,
        gpu_preferred=False,
        strengths=["256K native context", "instruction tuned", "multilingual"],
        best_for=["long documents on light hardware", "instruction following"],
        native_tools=True,
        internal=True,
        bytes_per_kv_token=3072,
        attention_arch="transformer",
    ),
    ModelCatalogEntry(
        # TODO: verify Ollama tag — `qwen3:14b` is plausible but unverified.
        id="qwen3-14b",
        preset="balanced",
        ollama_model="qwen3:14b",
        litellm_model="ollama_chat/qwen3:14b",
        label="Qwen3 14B",
        download_size_gb=9.0,
        context_window="32K",
        context_tokens=32768,
        recommended_ram_min_gb=24,
        recommended_ram_max_gb=32,
        min_disk_gb=14,
        cpu_friendly=False,
        gpu_preferred=True,
        strengths=["best dense Qwen3 for 24 GB", "tool calling", "128K via YaRN"],
        best_for=["everyday chat on 24 GB unified memory", "tools", "general use"],
        native_tools=True,
        internal=True,
        bytes_per_kv_token=5120,
        attention_arch="transformer",
    ),
    ModelCatalogEntry(
        # Verified absent on Ollama 0.18.0 (2026-04-28): `ollama pull qwen3:30b-a3b-instruct-2507`
        # returns "pull model manifest: file does not exist". Ollama's official
        # qwen3 library doesn't include the Instruct-2507 variant under that
        # tag; HuggingFace mirror exists at hf.co/Qwen/Qwen3-30B-A3B-Instruct-2507-GGUF
        # and Ollama can pull from HF via `ollama pull hf.co/...` syntax.
        # Kept internal=True (and excluded from user picker) until either:
        # (a) the official Ollama library adds the tag, or
        # (b) the catalog grows a separate `pull_url` field for HF mirrors and
        #     the canonical-chat-model probe (ADR 012) supports HF-pulled tags.
        # This entry was originally added as the v1 canonical chat-model
        # candidate; the canonical role is now Qwen3-14B per ADR 010 §"Issue 4"
        # until per-machine selection (ADR 012) lands.
        id="qwen3-30b-a3b-instruct-2507",
        preset="best-local",
        ollama_model="qwen3:30b-a3b-instruct-2507",
        litellm_model="ollama_chat/qwen3:30b-a3b-instruct-2507",
        label="Qwen3 30B-A3B Instruct 2507 (unavailable on Ollama 0.18.0)",
        download_size_gb=18.0,
        context_window="256K",
        context_tokens=262144,
        recommended_ram_min_gb=24,
        recommended_ram_max_gb=48,
        min_disk_gb=26,
        cpu_friendly=False,
        gpu_preferred=True,
        strengths=["MoE 30B/3B-active", "256K native", "tool calling", "candidate v1 chat model"],
        best_for=["best local chat", "long conversations", "tools"],
        native_tools=True,
        internal=True,
        bytes_per_kv_token=4096,
        attention_arch="transformer",
    ),
    ModelCatalogEntry(
        # TODO: verify Ollama tag — IBM Granite 4.0 tag conventions vary
        # (`granite4:h-micro`, `granite4-moe:micro`, `granite4:tiny-h`, etc.).
        id="granite-4-h-micro",
        preset="plumbing",
        ollama_model="granite4:h-micro",
        litellm_model="ollama_chat/granite4:h-micro",
        label="Granite 4.0 H-Micro",
        download_size_gb=2.0,
        context_window="32K",
        context_tokens=32768,
        recommended_ram_min_gb=8,
        recommended_ram_max_gb=16,
        min_disk_gb=4,
        cpu_friendly=True,
        gpu_preferred=False,
        strengths=["always-on classifier", "Apache 2.0", "ISO-42001 certified"],
        best_for=["plumbing/dispatcher classifier slot"],
        native_tools=False,
        internal=True,
        bytes_per_kv_token=256,
        attention_arch="mamba",
    ),
    ModelCatalogEntry(
        id="granite-4-h-tiny",
        preset="plumbing",
        ollama_model="granite4:h-tiny",
        litellm_model="ollama_chat/granite4:h-tiny",
        label="Granite 4.0 H-Tiny",
        download_size_gb=4.0,
        context_window="128K",
        context_tokens=131072,
        recommended_ram_min_gb=12,
        recommended_ram_max_gb=24,
        min_disk_gb=6,
        cpu_friendly=True,
        gpu_preferred=False,
        strengths=["plumbing", "Apache 2.0", "hybrid Mamba/attention"],
        best_for=["plumbing/dispatcher mid-tier"],
        native_tools=False,
        internal=True,
        bytes_per_kv_token=512,
        attention_arch="mamba",
    ),
    ModelCatalogEntry(
        id="granite-4-h-small",
        preset="plumbing",
        ollama_model="granite4:h-small",
        litellm_model="ollama_chat/granite4:h-small",
        label="Granite 4.0 H-Small",
        download_size_gb=18.0,
        context_window="128K",
        context_tokens=131072,
        recommended_ram_min_gb=32,
        recommended_ram_max_gb=48,
        min_disk_gb=24,
        cpu_friendly=False,
        gpu_preferred=True,
        strengths=["plumbing top-tier", "Apache 2.0", "hybrid Mamba/attention", "tool calling"],
        best_for=["plumbing/dispatcher top-tier"],
        native_tools=True,
        internal=True,
        bytes_per_kv_token=1024,
        attention_arch="mamba",
    ),
]

# Index for fast lookup
_CATALOG_BY_ID: Dict[str, ModelCatalogEntry] = {m.id: m for m in MODEL_CATALOG}


def _tool_mode_for(entry: ModelCatalogEntry) -> str:
    """Derive tool_mode from catalog entry flags.

    Tool-mode taxonomy:
      - native_qwen3: model exposes native function-calling in Qwen3 family format
        (the format the dispatcher's adapter standardises on)
      - adapted: tool calls are adapted via JSON-mode prompting through LiteLLM
      - excluded_from_tools: model is too small to reliably tool-call; the router
        excludes it from tool-using request classes
    """
    if entry.native_tools:
        return "native_qwen3"
    # Small models (< 3B params, heuristic: download < 2 GB) → excluded_from_tools
    if entry.download_size_gb < 2.0:
        return "excluded_from_tools"
    return "adapted"


def get_catalog() -> List[ModelCatalogEntry]:
    """Return the full model catalog."""
    return MODEL_CATALOG


def get_model_by_id(model_id: str) -> Optional[ModelCatalogEntry]:
    """Look up a model by its catalog ID."""
    return _CATALOG_BY_ID.get(model_id)


def get_model_by_litellm(litellm_model: str) -> Optional[ModelCatalogEntry]:
    """Look up a catalog entry by its LiteLLM model string."""
    if not litellm_model:
        return None
    for entry in MODEL_CATALOG:
        if entry.litellm_model == litellm_model:
            return entry
    return None


def effective_footprint_bytes(entry: ModelCatalogEntry, ctx_len_now: int) -> int:
    """Effective footprint = weights + bytes_per_kv_token × ctx_len_now.

    Used by the future memory-pressure auto-downgrade to ask "does this still
    fit in available RAM?" — a long Jira-ingest can balloon the chat-model
    footprint mid-session and a weights-only check would underestimate the
    swap risk.
    """
    weights = int(entry.download_size_gb * (1024 ** 3))
    return weights + entry.bytes_per_kv_token * max(0, ctx_len_now)


# ── Hardware Probe ───────────────────────────────────────────────────────────


def _detect_gpu() -> Tuple[Optional[str], Optional[float]]:
    """Detect GPU vendor and VRAM. Returns (vendor, vram_gb)."""
    system = platform.system()

    if system == "Darwin":
        # Apple Silicon has unified memory — GPU vendor is "apple",
        # VRAM is effectively the total RAM (shared).
        if platform.machine() == "arm64":
            return "apple", None
        return None, None

    # Try nvidia-smi for NVIDIA GPUs
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            result = subprocess.run(
                [nvidia_smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                vram_mb = float(result.stdout.strip().split("\n")[0])
                return "nvidia", round(vram_mb / 1024, 1)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass

    return None, None


def _get_total_ram_gb() -> float:
    """Get total system RAM in GB."""
    try:
        import psutil
        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except ImportError:
        # Fallback without psutil
        system = platform.system()
        if system == "Darwin":
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return round(int(result.stdout.strip()) / (1024 ** 3), 1)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
        elif system == "Linux":
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            kb = int(line.split()[1])
                            return round(kb / (1024 ** 2), 1)
            except (IOError, ValueError):
                pass
        return 0.0


def _get_free_disk_gb() -> float:
    """Get free disk space on the root volume in GB."""
    try:
        usage = shutil.disk_usage("/")
        return round(usage.free / (1024 ** 3), 1)
    except OSError:
        return 0.0


def classify_tier(ram_gb: float, gpu_vendor: Optional[str] = None) -> str:
    """Classify hardware into a tier based on RAM and GPU."""
    if ram_gb >= 48:
        return "workstation"
    if ram_gb >= 32:
        return "strong"
    if ram_gb >= 16:
        return "balanced"
    return "light"


def probe_hardware() -> HardwareProfile:
    """Detect local hardware profile."""
    system = platform.system().lower()
    os_name = {
        "darwin": "macos",
        "linux": "linux",
        "windows": "windows",
    }.get(system, system)

    arch = platform.machine()
    # Normalize arch
    if arch in ("AMD64", "x86_64"):
        arch = "x64"

    total_ram = _get_total_ram_gb()
    free_disk = _get_free_disk_gb()
    cpu_cores = os.cpu_count() or 1
    gpu_vendor, gpu_vram = _detect_gpu()
    is_apple_silicon = (os_name == "macos" and platform.machine() == "arm64")
    tier = classify_tier(total_ram, gpu_vendor)

    return HardwareProfile(
        os=os_name,
        arch=arch,
        total_ram_gb=total_ram,
        free_disk_gb=free_disk,
        cpu_cores=cpu_cores,
        gpu_vendor=gpu_vendor,
        gpu_vram_gb=gpu_vram,
        is_apple_silicon=is_apple_silicon,
        tier=tier,
    )


# ── Runtime Probe ────────────────────────────────────────────────────────────


def _detect_gpu_vram_used() -> Optional[float]:
    """Best-effort current GPU VRAM usage in GB (NVIDIA via nvidia-smi).

    Returns None on Apple Silicon (unified memory — caller uses RAM signals)
    and on machines without nvidia-smi.
    """
    if platform.system() == "Darwin":
        return None  # Apple Silicon: unified memory; no separate VRAM signal

    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return None
    try:
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            vram_mb = float(result.stdout.strip().split("\n")[0])
            return round(vram_mb / 1024, 2)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return None


async def _list_loaded_ollama_models(base_url: str) -> Tuple[bool, List[LoadedOllamaModel]]:
    """Fetch the list of models currently resident in Ollama via GET /api/ps.

    Returns (reachable, models). When unreachable, models is empty and the
    runtime-load endpoint can still report system signals.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{base_url}/api/ps")
            if resp.status_code != 200:
                return False, []
            data = resp.json()
            entries = data.get("models", []) or []
            loaded: List[LoadedOllamaModel] = []
            for entry in entries:
                loaded.append(LoadedOllamaModel(
                    name=entry.get("name", ""),
                    size=int(entry.get("size", 0) or 0),
                    size_vram=int(entry.get("size_vram", 0) or 0),
                    expires_at=entry.get("expires_at"),
                ))
            return True, loaded
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError):
        return False, []
    except Exception as exc:
        logger.debug("Ollama /api/ps unexpected error: %s", _sanitize_for_log(exc))
        return False, []


async def probe_runtime_load(base_url: str = DEFAULT_OLLAMA_BASE_URL) -> RuntimeLoad:
    """Snapshot the current runtime load — RAM/swap/GPU + Ollama-loaded models.

    Pure-Python today. The macOS branch graduates to a Tauri-side native
    helper after ADR 003 lands — `vm_stat` parsing is brittle across macOS
    versions. Until then, psutil gives a correct-shaped signal, just slightly
    noisier than the eventual native version on Apple Silicon.
    """
    base_url = _normalize_and_validate_ollama_base_url(base_url)

    try:
        import psutil
        vmem = psutil.virtual_memory()
        smem = psutil.swap_memory()
        total_gb = round(vmem.total / (1024 ** 3), 2)
        avail_gb = round(vmem.available / (1024 ** 3), 2)
        used_gb = round(vmem.used / (1024 ** 3), 2)
        ram_pct = float(vmem.percent)
        swap_total_gb = round(smem.total / (1024 ** 3), 2)
        swap_used_gb = round(smem.used / (1024 ** 3), 2)
        swap_pct = float(smem.percent)
    except ImportError:
        total_gb = _get_total_ram_gb()
        avail_gb = 0.0
        used_gb = 0.0
        ram_pct = 0.0
        swap_total_gb = 0.0
        swap_used_gb = 0.0
        swap_pct = 0.0

    gpu_vendor, gpu_vram_total = _detect_gpu()
    gpu_vram_used = _detect_gpu_vram_used()

    reachable, loaded = await _list_loaded_ollama_models(base_url)

    return RuntimeLoad(
        timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        total_ram_gb=total_gb,
        available_ram_gb=avail_gb,
        used_ram_gb=used_gb,
        ram_pct=ram_pct,
        swap_total_gb=swap_total_gb,
        swap_used_gb=swap_used_gb,
        swap_pct=swap_pct,
        gpu_vendor=gpu_vendor,
        gpu_vram_total_gb=gpu_vram_total,
        gpu_vram_used_gb=gpu_vram_used,
        loaded_models=loaded,
        ollama_reachable=reachable,
    )


async def probe_runtime(base_url: str = DEFAULT_OLLAMA_BASE_URL) -> RuntimeStatus:
    """Check if Ollama is installed and running."""
    base_url = _normalize_and_validate_ollama_base_url(base_url)
    status = RuntimeStatus(base_url=base_url)

    # 1. Check if ollama binary exists
    ollama_path = shutil.which("ollama")
    status.installed = ollama_path is not None

    # 2. Try to reach the API
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{base_url}/api/version")
            if resp.status_code == 200:
                status.running = True
                status.reachable = True
                data = resp.json()
                status.version = data.get("version")
            else:
                # Some Ollama versions return 200 on root but not /api/version
                # Try the root endpoint as fallback
                resp2 = await client.get(base_url)
                if resp2.status_code == 200:
                    status.running = True
                    status.reachable = True
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError):
        pass
    except Exception as exc:
        logger.debug("Ollama probe unexpected error: %s", exc)

    return status


# ── Installed Models ─────────────────────────────────────────────────────────


async def list_installed_models(base_url: str = DEFAULT_OLLAMA_BASE_URL) -> List[Dict[str, Any]]:
    """List models currently downloaded in Ollama via GET /api/tags."""
    base_url = _normalize_and_validate_ollama_base_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(f"{base_url}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("models", [])
    except (httpx.ConnectError, httpx.TimeoutException):
        logger.debug("Cannot reach Ollama at %s to list models", _sanitize_for_log(base_url))
    except Exception as exc:
        logger.debug("Error listing Ollama models: %s", _sanitize_for_log(exc))
    return []


def _is_model_installed(ollama_model: str, installed: List[Dict[str, Any]]) -> bool:
    """Check if a model name matches any installed model."""
    # Ollama model names can be "qwen3:8b" or "qwen3:latest"
    # Installed models have "name" field like "qwen3:8b"
    target = ollama_model.lower()
    for m in installed:
        name = m.get("name", "").lower()
        if name == target:
            return True
        # Also check without tag (e.g. "qwen3" matches "qwen3:latest")
        if ":" in target:
            base = target.split(":")[0]
            if name == base or name.startswith(base + ":"):
                # Only match if tags align
                pass
    return False


# ── Recommendation Engine ────────────────────────────────────────────────────


def score_model(
    model: ModelCatalogEntry,
    hw: HardwareProfile,
    installed_names: List[str],
    active_model_id: Optional[str] = None,
) -> ModelRecommendation:
    """Score a model against hardware profile and return a recommendation."""

    # Disk check — hard block
    required_disk = model.download_size_gb * 1.25 + 2
    if hw.free_disk_gb > 0 and hw.free_disk_gb < required_disk:
        return ModelRecommendation(
            model_id=model.id,
            preset=model.preset,
            label=model.label,
            ollama_model=model.ollama_model,
            litellm_model=model.litellm_model,
            download_size_gb=model.download_size_gb,
            context_window=model.context_window,
            strengths=model.strengths,
            best_for=model.best_for,
            recommended_ram="%d\u2013%d GB" % (model.recommended_ram_min_gb, model.recommended_ram_max_gb),
            native_tools=model.native_tools,
            tool_mode=_tool_mode_for(model),
            compatibility="unsupported",
            score=0,
            recommended=False,
            reason="Not enough disk space (need %.1f GB free)" % required_disk,
            installed=model.ollama_model.lower() in installed_names,
            active=(model.id == active_model_id and model.ollama_model.lower() in installed_names),
        )

    # RAM check
    ram = hw.total_ram_gb
    model_size = model.download_size_gb
    has_gpu = hw.gpu_vendor is not None

    if has_gpu or hw.is_apple_silicon:
        # GPU / Apple Silicon — unified memory or dedicated VRAM
        usable = hw.gpu_vram_gb if hw.gpu_vram_gb else ram
        if usable >= 2.0 * model_size:
            compat = "great"
            base_score = 90
        elif usable >= 1.5 * model_size:
            compat = "good"
            base_score = 70
        elif usable >= 1.2 * model_size:
            compat = "warning"
            base_score = 40
        else:
            compat = "unsupported"
            base_score = 0
    else:
        # CPU-only
        if ram >= 3.0 * model_size:
            compat = "great"
            base_score = 90
        elif ram >= 2.5 * model_size:
            compat = "good"
            base_score = 70
        elif ram >= 2.0 * model_size:
            compat = "warning"
            base_score = 40
        else:
            compat = "unsupported"
            base_score = 0

    # Bonuses
    score = base_score
    # cpu_friendly bonus only for pure CPU machines (not Apple Silicon, which has unified memory GPU)
    if model.cpu_friendly and not has_gpu and not hw.is_apple_silicon:
        score += 10
    if ram < model.recommended_ram_min_gb:
        score -= 20
    # On strong hardware (32+ GB), prefer higher-context models so they rank above tiny ones
    if hw.tier in ("strong", "workstation") and model.context_tokens >= 131072:
        score += 8
    # Clamp
    score = max(0, min(100, score))

    # Reason
    if compat == "great":
        reason_parts = ["Recommended"]
        if hw.is_apple_silicon:
            reason_parts.append("fits your %.0f GB unified memory" % ram)
        elif has_gpu:
            reason_parts.append("fits your %.0f GB RAM with GPU" % ram)
        else:
            reason_parts.append("fits your %.0f GB RAM" % ram)
        reason = " \u2014 ".join(reason_parts)
    elif compat == "good":
        reason = "Compatible with your hardware"
    elif compat == "warning":
        reason = "May be slow \u2014 your RAM is near the minimum"
    else:
        reason = "Not enough resources for this model"

    installed = model.ollama_model.lower() in installed_names

    return ModelRecommendation(
        model_id=model.id,
        preset=model.preset,
        label=model.label,
        ollama_model=model.ollama_model,
        litellm_model=model.litellm_model,
        download_size_gb=model.download_size_gb,
        context_window=model.context_window,
        strengths=model.strengths,
        best_for=model.best_for,
        recommended_ram="%d\u2013%d GB" % (model.recommended_ram_min_gb, model.recommended_ram_max_gb),
        native_tools=model.native_tools,
        tool_mode=_tool_mode_for(model),
        compatibility=compat,
        score=score,
        recommended=False,  # Set later by build_catalog
        reason=reason,
        installed=installed,
        active=(model.id == active_model_id and installed),
    )


async def build_catalog(
    hw: HardwareProfile,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    active_model_id: Optional[str] = None,
    include_internal: bool = False,
) -> List[ModelRecommendation]:
    """Build the model catalog with recommendations.

    By default, internal entries (unverified Ollama tags) are filtered out so
    the user-facing chat picker only sees pickable chat models. Callers that
    need the full catalog universe (e.g. for footprint planning) pass
    `include_internal=True`.
    """
    installed = await list_installed_models(base_url)
    installed_names = [m.get("name", "").lower() for m in installed]

    recommendations = []
    for model in MODEL_CATALOG:
        if model.internal and not include_internal:
            continue
        rec = score_model(model, hw, installed_names, active_model_id)
        recommendations.append(rec)

    # Sort by score descending
    recommendations.sort(key=lambda r: r.score, reverse=True)

    # Mark top 3 non-unsupported as recommended
    count = 0
    for rec in recommendations:
        if rec.compatibility != "unsupported" and count < 3:
            rec.recommended = True
            count += 1

    return recommendations


# ── Model Pull (Streaming) ──────────────────────────────────────────────────

async def pull_model_stream(model: str, base_url: str = DEFAULT_OLLAMA_BASE_URL):
    """Pull a model from Ollama, yielding SSE-formatted progress lines.

    Yields strings like: 'data: {"status": "pulling manifest"}\n\n'
    """
    base_url = _normalize_and_validate_ollama_base_url(base_url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
        async with client.stream(
            "POST",
            f"{base_url}/api/pull",
            json={"name": model, "stream": True},
        ) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                yield 'data: %s\n\n' % json.dumps({
                    "status": "error",
                    "error": "Ollama returned status %d: %s" % (resp.status_code, error_body.decode()[:200]),
                })
                return

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    yield "data: %s\n\n" % json.dumps(data)
                except json.JSONDecodeError:
                    continue

    yield 'data: {"status": "done"}\n\n'


# ── Model Delete ─────────────────────────────────────────────────────────────


async def delete_model(model: str, base_url: str = DEFAULT_OLLAMA_BASE_URL) -> bool:
    """Delete a model from Ollama. Returns True on success."""
    import json as _json
    base_url = _normalize_and_validate_ollama_base_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.request(
                "DELETE",
                f"{base_url}/api/delete",
                content=_json.dumps({"name": model}),
                headers={"Content-Type": "application/json"},
            )
            return resp.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


# ── Model Warm-up ────────────────────────────────────────────────────────────


# Single keep-alive value — keep loaded for half an hour after last use, then
# evict. Matches today's single-active-model semantics. A future memory-pressure
# auto-downgrade (consuming `probe_runtime_load()` + `effective_footprint_bytes()`)
# may want a shorter keep-alive on overflow events, but that's a runtime
# decision, not a static policy table.
DEFAULT_KEEP_ALIVE = "30m"


async def warm_up_model(model: str, base_url: str = DEFAULT_OLLAMA_BASE_URL) -> bool:
    """Send a tiny prompt to keep model loaded in Ollama memory."""
    base_url = _normalize_and_validate_ollama_base_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "keep_alive": DEFAULT_KEEP_ALIVE,
                },
            )
            return resp.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


# ── Model Test ───────────────────────────────────────────────────────────────


async def test_model(model: str, base_url: str = DEFAULT_OLLAMA_BASE_URL) -> TestResponse:
    """Quick validation that a model works end-to-end."""
    import time
    base_url = _normalize_and_validate_ollama_base_url(base_url)

    # Determine tool_mode from catalog (if known)
    catalog_entry = None
    for entry in MODEL_CATALOG:
        if entry.ollama_model == model:
            catalog_entry = entry
            break
    tool_mode = _tool_mode_for(catalog_entry) if catalog_entry else "adapted"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            start = time.monotonic()
            resp = await client.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say hello in one sentence."}],
                    "stream": False,
                },
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)

            if resp.status_code != 200:
                return TestResponse(
                    success=False,
                    error="Ollama returned status %d" % resp.status_code,
                )

            data = resp.json()
            message = data.get("message", {})
            text = message.get("content", "")[:200]

            # Calculate tokens/second from Ollama's metrics
            eval_count = data.get("eval_count", 0)
            eval_duration_ns = data.get("eval_duration", 0)
            tps = 0.0
            if eval_duration_ns > 0 and eval_count > 0:
                tps = round(eval_count / (eval_duration_ns / 1e9), 1)

            return TestResponse(
                success=True,
                response_text=text,
                latency_ms=elapsed_ms,
                tokens_per_second=tps,
                tool_mode=tool_mode,
            )
    except httpx.TimeoutException:
        return TestResponse(success=False, error="Request timed out (120s)")
    except httpx.ConnectError:
        return TestResponse(success=False, error="Cannot connect to Ollama")
    except Exception as exc:
        return TestResponse(success=False, error=str(exc)[:200])


# ── Active Model Config ─────────────────────────────────────────────────────


def get_active_local_model() -> Optional[Dict[str, str]]:
    """Read the active local model from workspace config.json."""
    from config import get_settings

    settings = get_settings()
    config_path = settings.workspace_path / "app" / "config.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path) as f:
            config = json.load(f)
        local = config.get("local_model")
        if local and local.get("active"):
            return local
    except (json.JSONDecodeError, IOError):
        pass
    return None


def set_active_local_model(
    model_id: str,
    litellm_model: str,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
) -> None:
    """Write the active local model to workspace config.json atomically.

    Uses `locked_config_update` from `services._config_io` so concurrent
    writers don't drop each other's updates via a read-modify-write race.
    """
    from config import get_settings
    from services._config_io import locked_config_update

    settings = get_settings()
    config_path = settings.workspace_path / "app" / "config.json"
    with locked_config_update(config_path) as config:
        config["local_model"] = {
            "active": True,
            "model_id": model_id,
            "litellm_model": litellm_model,
            "base_url": _normalize_and_validate_ollama_base_url(base_url),
        }


def clear_active_local_model() -> None:
    """Mark the active local model as inactive in workspace config.json.

    Goes through `locked_config_update` for the same reason as
    `set_active_local_model` — the read-modify-write race exists here too.
    Skip-on-unchanged keeps the mtime stable when there was no `local_model`
    key to clear (no-op write avoided).
    """
    from config import get_settings
    from services._config_io import locked_config_update

    settings = get_settings()
    config_path = settings.workspace_path / "app" / "config.json"
    if not config_path.exists():
        return
    with locked_config_update(config_path) as config:
        if "local_model" in config:
            config["local_model"]["active"] = False
