# Recovery.spec — the lifeline (recovery_ui.py), frozen as recovery.exe.
# Onedir (contract §6: no onefile temp extraction), windowed. Unlike the
# supervisor and host this one BUNDLES PySide6 — it must be able to open
# its UI with the app runtime completely dead.
a = Analysis(
    ['recovery_ui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['numpy', 'flask', 'yt_dlp', 'streamlink'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='recovery',
    debug=False, bootloader_ignore_signals=False, strip=False,
    upx=False, console=False,
    contents_directory='VideoGrabberRecovery_internal',
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='recovery')
