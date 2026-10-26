from __future__ import annotations


def message_is_current(message_version, current_version) -> bool:
    """两边都空不算更旧。消息严格更旧则丢弃。"""
    if message_version is None and current_version is None:
        return True
    if message_version is None:
        return False
    if current_version is None:
        return True
    return int(message_version) >= int(current_version)
