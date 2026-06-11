#!/usr/bin/env python3
"""飞书告警(lark-cli im +messages-send)。

chat_ids 为空或 lark-cli 缺失时静默跳过——无飞书环境照常可用。
target_id 以 ou_ 开头走 --user-id(私聊),否则 --chat-id(群)。
"""
from __future__ import annotations

import shutil
import subprocess

_SEV_ICON = {"info": "ℹ️", "warn": "⚠️", "error": "❌", "success": "✅"}


def send_feishu_message(target_id: str, title: str, content: str,
                        severity: str = "info", log=None) -> bool:
    exe = shutil.which("lark-cli")
    if not exe:
        if log:
            log.warning("lark-cli 不可用,跳过飞书通知")
        return False
    text = f"{_SEV_ICON.get(severity, '')} {title}\n{content}".strip()
    id_flag = "--user-id" if target_id.startswith("ou_") else "--chat-id"
    cmd = [exe, "im", "+messages-send", id_flag, target_id, "--text", text]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        if log:
            log.warning(f"飞书通知异常: {e}")
        return False
    if r.returncode != 0 and log:
        log.warning(f"飞书通知失败: {(r.stderr or r.stdout).strip()[:200]}")
    return r.returncode == 0


def notify(policy: dict, event: str, title: str, content: str,
           severity: str = "info", log=None) -> int:
    """按 escalation_policy.notify_on 过滤事件并发送;返回成功条数。"""
    esc = (policy or {}).get("escalation_policy") or {}
    if event not in (esc.get("notify_on") or []):
        return 0
    sent = 0
    for cid in esc.get("feishu_chat_ids") or []:
        if send_feishu_message(cid, title, content, severity, log=log):
            sent += 1
    return sent
