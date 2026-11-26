from copy import deepcopy

from src.utils.serial import content_digest


def expected_digest(result):
    """原摘要：对完整结果（桶记录 + 检证结论）做规范化 SHA-256。"""
    return content_digest(result)


def verify_digest(result, digest):
    """桶记录和结论都要与原摘要一致才算通过。"""
    return bool(digest) and content_digest(result) == digest


def well_formed(result):
    """修复件至少要保持结果结构，且明细行是字典列表。"""
    if not isinstance(result, dict):
        return False
    if not isinstance(result.get("rows"), list) or "summary" not in result or "metrics" not in result:
        return False
    return all(isinstance(row, dict) for row in result["rows"])


def drop_bucket_rows(result, count):
    """模拟已签批次桶证据缺行：抽走若干条「目录追踪」桶记录，其余行保留原序。"""
    damaged = deepcopy(result)
    kept, dropped = [], 0
    for row in damaged["rows"]:
        if dropped < count and row.get("类别") == "目录追踪":
            dropped += 1
            continue
        kept.append(row)
    damaged["rows"] = kept
    return damaged, dropped
