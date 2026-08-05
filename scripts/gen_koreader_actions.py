#!/usr/bin/env python3
"""Regenerate the plugin's koreader_actions.lua from a KOReader checkout.

    ./scripts/gen_koreader_actions.py ~/src/koreader

Takes the settingsList entries whose category is "none", meaning the event
fires with no argument, which is exactly what the mapper can send through
koreader.sh event <Name>.
"""
import re
import sys
from pathlib import Path

SECTIONS = [('general', 'General'), ('reader', 'Reader'), ('rolling', 'Reader (EPUB)'),
            ('paging', 'Reader (PDF)'), ('filemanager', 'File browser'),
            ('screen', 'Screen'), ('device', 'Device'), ('', 'Other')]
DEST = Path(__file__).resolve().parent.parent / \
    'koreader-plugin/hidpassthrough.koplugin/koreader_actions.lua'


def lua_quote(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = (Path(sys.argv[1]) / 'frontend/dispatcher.lua').read_text(encoding='utf-8')
    body = src[src.index('local settingsList = {'):src.index('local dispatcher_menu_order')]

    by = {}
    for m in re.finditer(r'^\s{4}(\w+)\s*=\s*\{(.*?)\},?\s*$', body, re.M):
        attrs = m.group(2)
        cat = re.search(r'category\s*=\s*"(\w+)"', attrs)
        ev = re.search(r'event\s*=\s*"(\w+)"', attrs)
        title = re.search(r'title\s*=\s*_\("([^"]+)"\)', attrs)
        if not (cat and ev and title) or cat.group(1) != 'none':
            continue
        section = next((s for s, _ in SECTIONS
                        if s and re.search(r'\b%s\s*=\s*true' % s, attrs)), '')
        by.setdefault(section, []).append((ev.group(1), title.group(1)))

    out = ['-- Generated from KOReader frontend/dispatcher.lua, the category="none"',
           '-- entries whose event takes no argument. Regenerate with',
           '-- scripts/gen_koreader_actions.py when KOReader gains actions.',
           'return {']
    total = 0
    for key, label in SECTIONS:
        if key not in by:
            continue
        out.append('    { section = %s, actions = {' % lua_quote(label))
        for ev, title in sorted(by[key], key=lambda x: x[1].lower()):
            out.append('        { event = %s, title = %s },' % (lua_quote(ev), lua_quote(title)))
            total += 1
        out.append('    } },')
    out.append('}')
    DEST.write_text('\n'.join(out) + '\n', encoding='utf-8')
    print('wrote %d actions to %s' % (total, DEST))


if __name__ == '__main__':
    main()
