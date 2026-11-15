"""离线 IPFIX 接入：一次捕获 -> 可追回原始字节的 SQLite 记录库。

公开 API：
    import_capture(input_jsonl, db_path, *, batch_size=2000,
                   max_templates_per_scope=1024, cache_kb=8192) -> ImportReport
    ResultStore(db_path).query_records(...) / .trace_field(...)
    inspect_capture(input_jsonl, ...) -> 只解析不落盘（用于演练/诊断）
    IpfixInputError / TargetExistsError / ConfigError / IpfixDatabaseError
"""
from .errors import (
    ConfigError,
    Failure,
    IpfixDatabaseError,
    IpfixError,
    IpfixInputError,
    TargetExistsError,
)
from .storage import DEFAULT_BATCH_SIZE, DEFAULT_CACHE_KB, ImportReport, import_capture
from .store import ResultStore

__all__ = [
    "import_capture",
    "ResultStore",
    "ImportReport",
    "IpfixError",
    "IpfixInputError",
    "TargetExistsError",
    "ConfigError",
    "IpfixDatabaseError",
    "Failure",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_CACHE_KB",
]
