"""Apply the release's strict unified source patch without requiring Git.

Every context line and hunk size is checked before any file is written. Paths
must stay beneath the source root. No fuzzy matching, offsets or binary patches.
"""
from pathlib import Path
import re

def apply(root: Path, patch: Path):
    root=root.resolve();lines=patch.read_text(encoding='utf-8').splitlines(keepends=True)
    plan=[];i=0
    while i<len(lines):
        if not lines[i].startswith('diff --git '):i+=1;continue
        i+=1
        while i<len(lines) and not lines[i].startswith('--- '):i+=1
        if i>=len(lines):raise ValueError('missing old file header')
        old=lines[i][4:].strip();new=lines[i+1][4:].strip();i+=2
        relative=(new if new!='/dev/null' else old)[2:]
        path=(root/relative).resolve()
        if not path.is_relative_to(root):raise ValueError('unsafe patch path')
        source=path.read_bytes().decode('utf-8').replace('\r\n','\n').splitlines(keepends=True) if path.exists() else []
        if old=='/dev/null' and path.exists():raise ValueError('new file already exists: '+relative)
        result=[];cursor=0
        while i<len(lines) and not lines[i].startswith('diff --git '):
            m=re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',lines[i])
            if not m:raise ValueError('unexpected patch line: '+lines[i][:80])
            at=max(0,int(m[1])-1);old_count=int(m[2] or 1);new_count=int(m[4] or 1)
            if at<cursor:raise ValueError('overlapping hunks')
            result.extend(source[cursor:at]);cursor=at;i+=1;removed=added=0
            while i<len(lines) and not lines[i].startswith(('@@ ','diff --git ')):
                line=lines[i];i+=1;kind=line[:1];value=line[1:]
                if kind not in (' ','+','-'):raise ValueError('unsupported patch record')
                if i<len(lines) and lines[i].startswith('\\ No newline at end of file'):
                    value=value.removesuffix('\n');i+=1
                if kind in (' ','-'):
                    if cursor>=len(source) or source[cursor]!=value:raise ValueError(f'{relative}:{cursor+1}: context differs')
                    cursor+=1;removed+=1
                if kind in (' ','+'):result.append(value);added+=1
            if (removed,added)!=(old_count,new_count):raise ValueError('hunk sizes differ')
        result.extend(source[cursor:]);plan.append((path,None if new=='/dev/null' else ''.join(result).encode('utf-8')))
    for path,data in plan:
        if data is None:path.unlink()
        else:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    return len(plan)
