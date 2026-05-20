#!/usr/bin/env python3
"""
bid_llm_client.py - 多大模型统一客户端

封装 GLM / DeepSeek / Qwen / Kimi / Minimax / 豆包 的 OpenAI 兼容 API，
统一接口、重试、fallback 降级、token 计费统计。

模型配置支持以下可选字段（读自 bid_llm_config.json）：
  - force_temperature: float  — 该模型强制温度（如 kimi-k2.5 只能 0.6），覆盖调用传入
  - extra_body: dict           — 透传给 OpenAI SDK extra_body，用于关推理等（GLM/Kimi thinking=disabled / MiniMax reasoning_split=true）
  - strip_think_tag: bool      — 响应 content 中 &lt;think&gt;...&lt;/think&gt; 正则剥离（MiniMax 兜底）
  - disabled: bool             — 停用标记，分配池/test-all 会跳过（如豆包未充值）

用法（作为模块）:
    from bid_llm_client import LLMClient
    client = LLMClient.load()
    text = client.chat("glm", system_prompt, user_prompt, temperature=0.6)

用法（命令行自检）:
    python bid_llm_client.py --test-all
    python bid_llm_client.py --test glm
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 兜底剥 <think>...</think>（含嵌套/跨行；无内容 think 标签也剥）
_THINK_TAG_RE = re.compile(r"<think\b[^>]*>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


# ─── 配置加载 ─────────────────────────────────────────────────────────

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from bid_common import get_bid_gen_home as _get_bid_gen_home  # noqa: E402
_BID_GEN_HOME = _get_bid_gen_home()
CONFIG_PATH = _BID_GEN_HOME / "configs" / "bid_llm_config.json"


def _load_env():
    """自动加载 .env（GLM_API_KEY / DEEPSEEK_API_KEY 等）"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for p in [
        Path(os.environ.get("LLM_ENV_FILE", "")),
        Path.home() / ".env",
    ]:
        if p and p.is_file():
            load_dotenv(p, override=False)
            return


_load_env()


# ─── 数据结构 ─────────────────────────────────────────────────────────

@dataclass
class LLMUsage:
    """单次调用 token 统计"""
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class LLMResponse:
    text: str
    usage: LLMUsage
    model: str
    attempt: int = 1
    fallback_from: Optional[str] = None


@dataclass
class LLMStats:
    """累计使用统计"""
    calls: list[LLMUsage] = field(default_factory=list)

    def add(self, u: LLMUsage):
        self.calls.append(u)

    def summary(self) -> dict:
        by_model: dict[str, dict] = {}
        for c in self.calls:
            d = by_model.setdefault(c.model, {"calls": 0, "prompt": 0, "completion": 0, "total": 0, "seconds": 0.0})
            d["calls"] += 1
            d["prompt"] += c.prompt_tokens
            d["completion"] += c.completion_tokens
            d["total"] += c.total_tokens
            d["seconds"] += c.elapsed_seconds
        return {
            "total_calls": len(self.calls),
            "total_tokens": sum(c.total_tokens for c in self.calls),
            "total_seconds": sum(c.elapsed_seconds for c in self.calls),
            "by_model": by_model,
        }


def _provider_key(endpoint: str) -> str:
    """从 endpoint 域名派生厂商标识,用于按厂商(而非按模型)限并发。

    硅基流动等聚合厂商下挂多个 model key,必须按厂商账户共用一道并发闸,
    否则每个 model key 各占一道,拦不住对单一账户的总并发 → 易触发 429。
    """
    host = (endpoint or "").lower()
    for kw, prov in (
        ("siliconflow", "siliconflow"),
        ("bigmodel", "zhipu"),
        ("deepseek", "deepseek"),
        ("dashscope", "dashscope"),
        ("aliyuncs", "dashscope"),
        ("moonshot", "moonshot"),
        ("minimaxi", "minimax"),
        ("minimax", "minimax"),
        ("volces", "volces"),
    ):
        if kw in host:
            return prov
    m = re.search(r"//([^/]+)", host)
    return (m.group(1) if m else host) or "default"


# ─── 客户端 ───────────────────────────────────────────────────────────

class LLMClient:
    """6 家大模型统一客户端（OpenAI 兼容协议）"""

    def __init__(self, config: dict, log: Optional[logging.Logger] = None):
        self.config = config
        self.models = config["models"]
        self.retry = config.get("retry", {"max_attempts": 3, "backoff_seconds": 5, "timeout_seconds": 180})
        self.fallback_order = config.get("fallback_order", list(self.models.keys()))
        self.log = log or logging.getLogger("bid_llm")
        self.stats = LLMStats()
        self._clients_cache: dict[str, object] = {}
        # 每家 provider 并发闸：避免批量投标把单家 API 压爆触发 429/529/超时。
        # 按厂商(endpoint 派生)分闸,聚合厂商(硅基流动)下多个 model key 共用一道。
        # 配置 max_concurrency 用厂商名: {"default": 3, "siliconflow": 6, "zhipu": 1, ...}
        mc_cfg = config.get("max_concurrency") or {}
        default_mc = int(mc_cfg.get("default", 3)) if isinstance(mc_cfg, dict) else int(mc_cfg or 3)
        self._provider_sems: dict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(default_mc)
        )
        if isinstance(mc_cfg, dict):
            for k, v in mc_cfg.items():
                if k == "default":
                    continue
                self._provider_sems[k] = threading.Semaphore(int(v))

    @classmethod
    def load(cls, config_path: Path = CONFIG_PATH, log: Optional[logging.Logger] = None) -> "LLMClient":
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        return cls(cfg, log=log)

    def _get_openai_client(self, model_key: str):
        """获取或创建 openai SDK client（懒加载，缓存）"""
        if model_key in self._clients_cache:
            return self._clients_cache[model_key]

        if model_key not in self.models:
            raise ValueError(f"未配置的模型: {model_key}")
        m = self.models[model_key]
        api_key = os.environ.get(m["api_key_env"], "")
        if not api_key:
            raise RuntimeError(f"环境变量 {m['api_key_env']} 未设置（模型 {model_key}）")

        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("请先安装 openai: pip install openai>=1.0")

        client = OpenAI(
            api_key=api_key,
            base_url=m["endpoint"],
            timeout=float(self.retry.get("timeout_seconds", 180)),
        )
        self._clients_cache[model_key] = client
        return client

    def chat(
        self,
        model_key: str,
        system: str,
        user: str,
        temperature: float = 0.6,
        max_tokens: Optional[int] = None,
        enable_fallback: bool = True,
    ) -> LLMResponse:
        """调用指定模型。失败时按 fallback_order 降级。"""
        tried: list[str] = []
        last_err: Optional[Exception] = None

        # 构造降级序列
        order = [model_key]
        if enable_fallback:
            for f in self.fallback_order:
                if f != model_key and f in self.models:
                    order.append(f)

        for idx, m_key in enumerate(order):
            tried.append(m_key)
            try:
                return self._call_with_retry(m_key, system, user, temperature, max_tokens,
                                             fallback_from=model_key if idx > 0 else None)
            except Exception as e:
                last_err = e
                self.log.warning(f"模型 {m_key} 调用失败: {e}")
                if not enable_fallback:
                    break
                if idx < len(order) - 1:
                    self.log.info(f"降级到下一模型 {order[idx + 1]}")

        raise RuntimeError(f"所有模型调用失败，尝试顺序: {tried}。最后错误: {last_err}")

    def _call_with_retry(
        self, model_key: str, system: str, user: str,
        temperature: float, max_tokens: Optional[int],
        fallback_from: Optional[str] = None,
    ) -> LLMResponse:
        m = self.models[model_key]
        client = self._get_openai_client(model_key)
        mt = max_tokens or m.get("max_tokens", 8000)
        if "force_temperature" in m:
            temperature = float(m["force_temperature"])

        max_attempts = int(self.retry.get("max_attempts", 3))
        backoff = float(self.retry.get("backoff_seconds", 5))

        last_err = None
        # 关推理等特殊参数走 extra_body 透传（如 GLM/Kimi thinking=disabled / MiniMax reasoning_split=true）
        extra_body = m.get("extra_body") or None
        strip_think = bool(m.get("strip_think_tag"))

        sem = self._provider_sems[_provider_key(m.get("endpoint", ""))]
        for attempt in range(1, max_attempts + 1):
            start = time.time()
            try:
                create_kwargs = dict(
                    model=m["model_name"],
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=temperature,
                    max_tokens=mt,
                )
                if extra_body:
                    create_kwargs["extra_body"] = extra_body
                with sem:
                    resp = client.chat.completions.create(**create_kwargs)
                elapsed = time.time() - start
                text = resp.choices[0].message.content or ""
                if strip_think and text:
                    text = _THINK_TAG_RE.sub("", text).lstrip()
                usage = resp.usage
                u = LLMUsage(
                    model=model_key,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    total_tokens=getattr(usage, "total_tokens", 0) or 0,
                    elapsed_seconds=elapsed,
                )
                self.stats.add(u)
                self.log.info(f"[{model_key}] ok  tokens={u.total_tokens}  耗时={elapsed:.1f}s  尝试={attempt}")
                return LLMResponse(text=text, usage=u, model=model_key,
                                   attempt=attempt, fallback_from=fallback_from)
            except Exception as e:
                last_err = e
                elapsed = time.time() - start
                self.log.warning(f"[{model_key}] 第 {attempt}/{max_attempts} 次失败 ({elapsed:.1f}s): {e}")
                if attempt < max_attempts:
                    time.sleep(backoff * attempt)

        raise RuntimeError(f"模型 {model_key} 重试 {max_attempts} 次后仍失败: {last_err}")


# ─── 命令行自检 ───────────────────────────────────────────────────────

def _setup_logger(verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("bid_llm")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    log.addHandler(h)
    return log


def _test_model(client: LLMClient, model_key: str) -> bool:
    """单模型连通性测试"""
    try:
        resp = client.chat(
            model_key=model_key,
            system="你是一个助手。",
            user="请用一句话回答：投标技术标的核心作用是什么？",
            temperature=0.5,
            max_tokens=200,
            enable_fallback=False,
        )
        print(f"  ✓ {model_key}: {resp.text[:80].strip()}...")
        print(f"    tokens={resp.usage.total_tokens}  耗时={resp.usage.elapsed_seconds:.1f}s")
        return True
    except Exception as e:
        print(f"  ✗ {model_key}: {e}")
        return False


_CONFIG_TEMPLATE = {
    "default_model": "glm",
    "fallback_order": ["glm", "deepseek", "qwen", "kimi", "minimax"],
    "retry": {"max_attempts": 3, "backoff_seconds": 5, "timeout_seconds": 180},
    "max_concurrency": {"default": 3, "siliconflow": 6, "zhipu": 1, "minimax": 2},
    "default_temperature": 0.6,
    "temperature_variants": [0.5, 0.7, 0.9],
    "models": {
        "glm": {
            "display_name": "智谱 GLM-4.6",
            "endpoint": "https://open.bigmodel.cn/api/paas/v4",
            "model_name": "glm-4.6",
            "api_key_env": "GLM_API_KEY",
            "max_tokens": 8000,
            "output_char_cap": 5500,
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "deepseek": {
            "display_name": "DeepSeek-V3.2",
            "endpoint": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat",
            "api_key_env": "DEEPSEEK_API_KEY",
            "max_tokens": 8000,
            "output_char_cap": 5000,
        },
        "qwen": {
            "display_name": "通义千问 Qwen3-Max",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model_name": "qwen3-max",
            "api_key_env": "DASHSCOPE_API_KEY",
            "max_tokens": 8000,
            "output_char_cap": 5500,
        },
        "kimi": {
            "display_name": "月之暗面 Kimi-K2.5",
            "endpoint": "https://api.moonshot.cn/v1",
            "model_name": "kimi-k2.5",
            "api_key_env": "MOONSHOT_API_KEY",
            "max_tokens": 8000,
            "output_char_cap": 5500,
            "force_temperature": 0.6,
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "minimax": {
            "display_name": "MiniMax-M2.5",
            "endpoint": "https://api.minimaxi.com/v1",
            "model_name": "MiniMax-M2.5",
            "api_key_env": "MINIMAX_API_KEY",
            "max_tokens": 8000,
            "output_char_cap": 5000,
            "extra_body": {"reasoning_split": True},
            "strip_think_tag": True,
        },
        "doubao": {
            "display_name": "字节豆包 Pro",
            "endpoint": "https://ark.cn-beijing.volces.com/api/v3",
            "model_name": "doubao-1-5-pro-32k-250115",
            "api_key_env": "ARK_API_KEY",
            "max_tokens": 8000,
            "output_char_cap": 5500,
            "disabled": True,
        },
    },
}


def _init_config(force: bool) -> int:
    if CONFIG_PATH.exists() and not force:
        print(f"配置已存在: {CONFIG_PATH}（加 --force 覆盖）", file=sys.stderr)
        return 1
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(_CONFIG_TEMPLATE, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"✓ 已写入 {CONFIG_PATH}")
    print("请检查 endpoint / model_name，并设置以下环境变量：")
    for _, m in _CONFIG_TEMPLATE["models"].items():
        print(f"  export {m['api_key_env']}=<your_key>")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="多模型客户端自检工具")
    p.add_argument("--test-all", action="store_true", help="测试所有配置的模型")
    p.add_argument("--test", help="测试指定单个模型（glm/deepseek/qwen/kimi/minimax/doubao）")
    p.add_argument("--list", action="store_true", help="列出配置的所有模型")
    p.add_argument("--init", action="store_true", help="生成 bid_llm_config.json 模板")
    p.add_argument("--force", action="store_true", help="配合 --init 覆盖已有配置")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    log = _setup_logger(args.verbose)

    if args.init:
        return _init_config(args.force)

    try:
        client = LLMClient.load(log=log)
    except Exception as e:
        print(f"加载配置失败: {e}", file=sys.stderr)
        return 1

    if args.list:
        print("已配置模型:")
        for k, m in client.models.items():
            key_set = "✓" if os.environ.get(m["api_key_env"]) else "✗"
            tag = " [停用]" if m.get("disabled") else ""
            print(f"  [{key_set}] {k:<10} {m['display_name']:<25} env={m['api_key_env']}{tag}")
        return 0

    if args.test:
        ok = _test_model(client, args.test)
        return 0 if ok else 1

    if args.test_all:
        print("测试所有模型连通性:")
        results = {}
        for k, m in client.models.items():
            if m.get("disabled"):
                print(f"  - {k}: 停用跳过")
                continue
            results[k] = _test_model(client, k)
        print("\n汇总:")
        for k, ok in results.items():
            print(f"  {'✓' if ok else '✗'} {k}")
        failed = [k for k, ok in results.items() if not ok]
        return 0 if not failed else 1

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
