# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs, collect_submodules


datas = []
binaries = []
hiddenimports = []

for package_name in (
    'rapidocr_onnxruntime',
    'onnxruntime',
    'pygetwindow',
    'pyautogui',
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

datas += collect_data_files('rapidocr_onnxruntime', include_py_files=False)
datas += collect_data_files('cv2', include_py_files=False)
binaries += collect_dynamic_libs('onnxruntime')
binaries += collect_dynamic_libs('cv2')
hiddenimports += collect_submodules('rapidocr_onnxruntime')
hiddenimports += ['cv2']

a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CoC Auto Clash',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

