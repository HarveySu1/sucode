"""配置层：YAML 多 profile 的加载与校验（tasks.md T2）。

顶层结构：default_profile + profiles 映射；每个 profile 四核心字段
protocol / model / base_url / api_key，可选 max_tokens 与 thinking。
base_url 留空走官方端点（由 Provider 层处理）；api_key 留空按
protocol 回退环境变量（spec C13）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

def default_config_path() -> Path:
    """配置文件默认路径（调用时解析，便于测试与未来 --config 参数）。"""
    return Path.home() / ".sucode" / "config.yaml"

# Anthropic API 硬性约束（见 tasks.md T2/T4 参考定位）
DEFAULT_MAX_TOKENS = 8192
DEFAULT_THINKING_BUDGET = 4096
MIN_THINKING_BUDGET = 1024

VALID_PROTOCOLS = ("anthropic", "openai")

# api_key 留空时按协议回退的环境变量（spec C13）
ENV_KEY_BY_PROTOCOL = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

EXAMPLE_TEMPLATE = """\
default_profile: deepseek

profiles:
  # OpenAI 兼容端点示例（DeepSeek/GLM/Qwen 等同理，改 base_url 即可）
  deepseek:
    protocol: openai
    model: deepseek-chat
    base_url: https://api.deepseek.com
    api_key: sk-xxx
  # Anthropic 官方示例（thinking 仅对 anthropic 协议生效）
  claude:
    protocol: anthropic
    model: claude-sonnet-4-5
    base_url: ""            # 留空走官方端点
    api_key: ""             # 留空读环境变量 ANTHROPIC_API_KEY
    max_tokens: 8192        # 可选，默认 8192
    thinking:               # 可选
      enabled: true
      budget_tokens: 4096   # 最小 1024，且必须小于 max_tokens
"""


class ConfigError(Exception):
    """配置加载/校验失败；message 面向用户，自带修复指引。"""


@dataclass
class ProfileConfig:
    """单个 profile 的规范化结果（api_key 已完成环境变量回退）。"""

    name: str
    protocol: str  # "anthropic" | "openai"
    model: str
    base_url: str  # 留空 = 官方端点
    api_key: str  # 可能为空，激活使用时才强校验
    max_tokens: int
    thinking_enabled: bool
    thinking_budget: int


@dataclass
class AppConfig:
    default_profile: str
    profiles: dict[str, ProfileConfig]

    def get(self, name: str | None = None) -> ProfileConfig:
        """取指定 profile（name 为空用 default_profile）；激活时强校验密钥。"""
        target = name or self.default_profile
        if target not in self.profiles:
            available = "、".join(sorted(self.profiles)) or "（无）"
            raise ConfigError(f"未找到 profile「{target}」，可用：{available}")
        profile = self.profiles[target]
        if not profile.api_key:
            env_name = ENV_KEY_BY_PROTOCOL[profile.protocol]
            raise ConfigError(
                f"profile「{target}」未配置 api_key，且环境变量 {env_name} 未设置；"
                f"请在配置文件中填写 api_key，或设置 {env_name}"
            )
        return profile


def _validate_profile(name: str, raw: object) -> ProfileConfig:
    """校验单个 profile 的结构与取值。"""
    if not isinstance(raw, dict):
        raise ConfigError(f"profile「{name}」的内容应为键值映射，当前为 {type(raw).__name__}")

    protocol = raw.get("protocol")
    if protocol not in VALID_PROTOCOLS:
        raise ConfigError(
            f"profile「{name}」的 protocol 必须是 {' 或 '.join(VALID_PROTOCOLS)}，当前为 {protocol!r}"
        )

    model = str(raw.get("model") or "").strip()
    if not model:
        raise ConfigError(f"profile「{name}」缺少必填字段 model")

    base_url = str(raw.get("base_url") or "").strip()

    # api_key 显式配置优先，留空回退环境变量
    api_key = str(raw.get("api_key") or "").strip()
    if not api_key:
        api_key = os.environ.get(ENV_KEY_BY_PROTOCOL[protocol], "").strip()

    max_tokens = raw.get("max_tokens", DEFAULT_MAX_TOKENS)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ConfigError(f"profile「{name}」的 max_tokens 应为正整数，当前为 {max_tokens!r}")

    thinking_raw = raw.get("thinking")
    if thinking_raw is None:
        thinking_raw = {}
    if not isinstance(thinking_raw, dict):
        raise ConfigError(f"profile「{name}」的 thinking 应为键值映射（enabled / budget_tokens）")
    if thinking_raw and protocol != "anthropic":
        raise ConfigError(
            f"profile「{name}」：thinking 仅支持 anthropic 协议"
            f"（OpenAI 端不做思考展示，见 spec O6）"
        )
    thinking_enabled = bool(thinking_raw.get("enabled", False))
    thinking_budget = thinking_raw.get("budget_tokens", DEFAULT_THINKING_BUDGET)
    if (
        isinstance(thinking_budget, bool)
        or not isinstance(thinking_budget, int)
        or thinking_budget < MIN_THINKING_BUDGET
    ):
        raise ConfigError(
            f"profile「{name}」的 thinking.budget_tokens 应为不小于 {MIN_THINKING_BUDGET} 的整数，"
            f"当前为 {thinking_budget!r}"
        )
    if thinking_enabled and thinking_budget >= max_tokens:
        raise ConfigError(
            f"profile「{name}」的 thinking.budget_tokens（{thinking_budget}）"
            f"必须小于 max_tokens（{max_tokens}）"
        )

    return ProfileConfig(
        name=name,
        protocol=protocol,
        model=model,
        base_url=base_url,
        api_key=api_key,
        max_tokens=max_tokens,
        thinking_enabled=thinking_enabled,
        thinking_budget=thinking_budget,
    )


def load_config(path: Path | None = None) -> AppConfig:
    """读取并校验配置文件；任何问题都以 ConfigError 抛出（含修复指引）。"""
    cfg_path = Path(path) if path else default_config_path()
    if not cfg_path.is_file():
        raise ConfigError(
            f"未找到配置文件：{cfg_path}\n"
            f"请创建该文件，写入如下内容后重试（按需修改）：\n\n{EXAMPLE_TEMPLATE}"
        )
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法 YAML：{cfg_path}\n{exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(
            f"配置文件顶层应为映射（default_profile + profiles）：{cfg_path}\n"
            f"参考结构：\n\n{EXAMPLE_TEMPLATE}"
        )

    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise ConfigError(f"配置缺少 profiles（至少定义一个 profile）：{cfg_path}\n\n{EXAMPLE_TEMPLATE}")
    profiles = {str(name): _validate_profile(str(name), p) for name, p in profiles_raw.items()}

    default_profile = raw.get("default_profile")
    if not isinstance(default_profile, str) or not default_profile.strip():
        raise ConfigError(f"配置缺少 default_profile 字段：{cfg_path}\n\n{EXAMPLE_TEMPLATE}")
    default_profile = default_profile.strip()
    if default_profile not in profiles:
        raise ConfigError(
            f"default_profile「{default_profile}」不存在，可用：{'、'.join(sorted(profiles))}"
        )
    return AppConfig(default_profile=default_profile, profiles=profiles)
