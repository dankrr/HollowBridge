"""Read-only, deliberately conservative Steam app-manifest inspection."""
import re
from pathlib import Path

_TOKEN = re.compile(r'\s*(?:(//[^\n]*(?:\n|$))|"((?:\\.|[^"\\])*)"|([{}]))')


def parse_vdf(text: str) -> dict:
    if len(text) > 4_000_000:
        raise ValueError('manifest too large')
    tokens = []
    pos = 0
    while pos < len(text):
        if not text[pos:].strip():
            break
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError('invalid manifest')
        pos = m.end()
        if m.group(1) is not None:
            continue
        if m.group(2) is not None:
            tokens.append(('value', re.sub(r'\\([\\"])', r'\1', m.group(2))))
        else:
            tokens.append((m.group(3), m.group(3)))
    def block(i, nested=False):
        result = {}
        while i < len(tokens):
            kind, key = tokens[i]
            if kind == '}':
                if not nested: raise ValueError('unexpected closing brace')
                return result, i + 1
            if kind != 'value' or i + 1 >= len(tokens): raise ValueError('invalid key')
            i += 1
            kind, value = tokens[i]
            if kind == '{': value, i = block(i + 1, True)
            elif kind == 'value': i += 1
            else: raise ValueError('invalid value')
            if key in result: raise ValueError('duplicate key')
            result[key] = value
        if nested: raise ValueError('missing closing brace')
        return result, i
    result, _ = block(0)
    return result


def inspect_manifest(path: str | Path, expected_appid: int) -> dict:
    path = Path(path)
    if not path.exists():
        return {'status': 'absent'}
    try:
        if path.stat().st_size > 4_000_000: raise ValueError('manifest too large')
        data = parse_vdf(path.read_text(encoding='utf-8'))['AppState']
        if int(data['appid']) != expected_appid: raise ValueError('appid mismatch')
        flags = int(data.get('StateFlags', 0))
        size = int(data.get('SizeOnDisk', 0))
        total = int(data.get('BytesToDownload', 0))
        done = int(data.get('BytesDownloaded', 0))
        depots = data.get('InstalledDepots', {})
        if not isinstance(depots, dict) or min(flags, size, total, done) < 0:
            raise ValueError('invalid state')
        # A Steam installed flag alone also occurs for empty 0-depot installs.
        if flags == 4 and size > 0 and depots:
            status = 'installed_with_content'
        elif flags == 4 and size == 0 and total == 0 and not depots:
            status = 'empty_install'
        else:
            status = 'pending_or_unknown'
        return {'status': status, 'state_flags': flags, 'size_on_disk': size,
                'bytes_to_download': total, 'bytes_downloaded': done,
                'installed_depot_count': len(depots)}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return {'status': 'unreadable'}
