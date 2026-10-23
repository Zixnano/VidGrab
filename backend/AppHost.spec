# AppHost.spec — the app host (app_host.py, the former launcher),
# frozen as VideoGrabberApp.exe. Onedir, windowed, thin: PySide6 /
# numpy / PIL / yt-dlp are loaded at runtime from
# runtime\site-packages, never bundled here.
a = Analysis(
    ['app_host.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Standard library modules missed by PyInstaller
        'http.cookies', 'logging.handlers', 'http.client', 
        'email.mime', 'urllib.request', 'email.utils',
        'optparse', 'getopt', 'shlex', 'base64', 
        'binascii', 'calendar', 'copy', 'csv',
        'hashlib', 'hmac', 'html', 'http', 'json',
        'mimetypes', 'netrc', 'os', 're', 'socket',
        'ssl', 'struct', 'sys', 'time', 'urllib',
        'uuid', 'xml', 'zipfile', 'zlib',
        # Third-party modules we are now bundling
        'requests', 'flask'
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['numpy', 'PIL', 'PySide6', 'yt_dlp', 'streamlink'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='VideoGrabberApp',
    debug=False, bootloader_ignore_signals=False, strip=False,
    upx=False, console=False,
    contents_directory='VideoGrabberApp_internal',
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='VideoGrabberApp')