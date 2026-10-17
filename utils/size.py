"""工具层：尺寸字符串解析与人类可读格式化。"""

from ..domain.constants import _SIZE_UNITS


def _parse_size_bytes(raw_size: str) -> int:
    """解析 BPCS ls 输出的大小字符串（如 1.5GB / 300KB / -）为字节数，失败返回 0。"""
    raw = (raw_size or "").strip().upper()
    if not raw or raw == "-":
        return 0
    try:
        for unit, mult in _SIZE_UNITS:
            if raw.endswith(unit):
                return int(float(raw[:-len(unit)]) * mult)
        if raw.endswith("B"):
            return int(float(raw[:-1]))
        return int(float(raw))
    except ValueError:
        return 0

def _human_size(b: int) -> str:
    """把字节数格式化为人类可读（含两位小数）；入参是**字节数**，非正数不带单位。

    只接受数字：调用方传的必须是已经算好的字节数。外部数据（BPCS / 网盘 API 返回的
    size 可能是字符串、None 或 float）请先走 _format_size，直接传进来会因
    `str <= 0` / `str >= threshold` 抛 TypeError，把整条目录树渲染带崩。"""
    if b <= 0:
        return f"{b:.0f}"
    for unit, threshold in _SIZE_UNITS:
        if b >= threshold:
            return f"{b / threshold:.2f} {unit}"
    return f"{b:.0f} B"

def _format_size(raw_size: str) -> str:
    """解析「尺寸字符串」后格式化；解析失败或非正数时原样回显输入。

    与 _human_size 不是重复实现，两者契约不同：_human_size 收字节数，这里收外部
    原始值（字符串 / None / float 都可能）。保留「非正数原样回显」还维持了历史显示：
    0 显示成 `0` 而不是 `0 B`、`None` 显示成 `None`。"""
    b = _parse_size_bytes(raw_size)
    if b <= 0:
        return raw_size
    return _human_size(b)

def _display_size_in_unit(expected: int) -> int:
    """推断 expected 字节数在 BPCS 里会用哪个显示单位（取不超过它的最大单位），

    供 `_size_matches` 按显示单位估算舍入误差。"""
    for _, threshold in _SIZE_UNITS:
        if expected >= threshold:
            return threshold
    return 1

def _size_matches(actual: int, expected: int) -> bool:
    """本地文件大小是否可视为「与云端一致」。

    BPCS `ls -l` 的体积是两位小数的可读值（`ConvertFileSize(size, 2)`），解析回来必带舍入
    误差，且误差上限由**显示单位**决定：B 精确、KB 约 5 字节、MB 约 5KB、GB 约 5MB。这里按
    云端大小推断显示单位（BPCS 取不超过该体积的最大单位），取该单位的 1% 作容差——既覆盖
    显示舍入，又不会像固定比例那样随体积放宽到几十 MB（半截文件会被放行）。
    expected <= 0 表示云端大小未知：这里只能放行，拦截交给调用方（未知大小按「本次下载期间
    是否被写过」判断残留）。"""
    if expected <= 0:
        return True
    if expected < 1024:
        # B 量级 BPCS 显示的就是精确字节数，只留整数截断余量；留 8 字节的宽地板会把
        # 「500B 少 5B」这类明显截断判成完整
        return abs(actual - expected) <= 2
    unit = _display_size_in_unit(expected)
    return abs(actual - expected) <= max(8, unit // 100)
