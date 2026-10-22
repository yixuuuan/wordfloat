#!/bin/bash
# 把源码组装成 dist/浮词.app，并打包成 dist/WordFloat-macOS-v<版本>.zip
# 用法：./build.sh            macOS 或 Linux 均可
set -euo pipefail
cd "$(dirname "$0")"

VERSION="$(/usr/bin/env python3 -c 'import plistlib;print(plistlib.load(open("macos/Info.plist","rb"))["CFBundleShortVersionString"])')"
APP="dist/浮词.app"
ZIP="dist/WordFloat-macOS-v${VERSION}.zip"

rm -rf dist && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp macos/Info.plist            "$APP/Contents/Info.plist"
printf 'APPL????'            > "$APP/Contents/PkgInfo"
cp macos/WordFloat             "$APP/Contents/MacOS/WordFloat"
cp src/wordfloat.py            "$APP/Contents/Resources/wordfloat.py"
cp data/wordfloat_dict.db.gz   "$APP/Contents/Resources/wordfloat_dict.db.gz"
cp assets/AppIcon.icns         "$APP/Contents/Resources/AppIcon.icns"
chmod 755 "$APP/Contents/MacOS/WordFloat"

if command -v ditto >/dev/null 2>&1; then
  (cd dist && ditto -c -k --keepParent "浮词.app" "$(basename "$ZIP")")
else
  # Linux：用 Python 打包，标记 UTF-8 文件名，保留可执行权限
  (cd dist && /usr/bin/env python3 - "$(basename "$ZIP")" <<'PY'
import os, sys, time, zipfile
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk("浮词.app"):
        dirs.sort()
        for name in [root] + [os.path.join(root, f) for f in sorted(files)]:
            st = os.stat(name)
            arc = name + "/" if os.path.isdir(name) else name
            # 用文件自身的修改时间：程序靠词典的时间戳判断是否重新解压
            info = zipfile.ZipInfo(arc, date_time=time.localtime(st.st_mtime)[:6])
            info.flag_bits |= 0x800
            info.create_system = 3
            info.external_attr = (st.st_mode & 0xFFFF) << 16 | (0x10 if os.path.isdir(name) else 0)
            if os.path.isdir(name):
                z.writestr(info, b"")
            else:
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(name, "rb") as f:
                    z.writestr(info, f.read())
PY
  )
fi
echo "$ZIP"
