# AppHost.spec — the app host (app_host.py, the former launcher),
# frozen as VideoGrabberApp.exe. Onedir, windowed, thin: PySide6 /
# numpy / PIL / yt-dlp are loaded at runtime from
# runtime\site-packages, never bundled here.
#
# Why hiddenimports is long: the heavy packages (yt_dlp, requests,
# flask) are EXCLUDED from the analysis and imported at runtime from
# runtime\site-packages, so PyInstaller never sees their import
# graphs. Every standard library module they touch dynamically must
# be named here, or the frozen host dies with ModuleNotFoundError
# the first time a runtime package reaches for it (http.cookies,
# logging.handlers, optparse and xml.etree all failed this way).
a = Analysis(
    ['app_host.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # --- XML (the current crash: yt_dlp.compat imports
        # xml.etree.ElementTree; the whole package chain plus the
        # C accelerators must come along) ---
        'xml', 'xml.etree', 'xml.etree.ElementTree',
        'xml.dom', 'xml.dom.minidom', 'xml.sax', 'xml.sax.handler',
        'xml.parsers.expat', 'pyexpat', '_elementtree',
        'xml.sax.saxutils', 'xml.sax.xmlreader',
        # --- HTTP, URLs, cookies, mail-shaped plumbing ---
        'http', 'http.client', 'http.cookies', 'http.cookiejar',
        'urllib', 'urllib.parse', 'urllib.request', 'urllib.error',
        'urllib.response', 'urllib.robotparser',
        'email', 'email.parser', 'email.feedparser', 'email.header',
        'email.utils', 'email.encoders', 'email.policy',
        'email.message', 'email.mime', 'email.mime.base',
        'email.mime.text', 'email.mime.multipart',
        'email.mime.message', 'email.mime.nonmultipart',
        'html', 'html.parser', 'html.entities',
        'ftplib', 'netrc', 'mimetypes', 'ipaddress',
        'socket', 'ssl', 'selectors',
        # --- Data formats, compression, hashing ---
        'json', 'json.decoder', 'json.encoder',
        'gzip', 'zlib', 'bz2', 'lzma', 'zipfile', 'tarfile',
        'hashlib', 'hmac', 'base64', 'binascii', 'secrets', 'uuid',
        'csv', 'configparser', 'plistlib', 'struct',
        # --- Logging and CLI plumbing ---
        'logging', 'logging.handlers', 'logging.config',
        'optparse', 'argparse', 'getopt', 'shlex',
        # --- System glue yt_dlp and requests reach for ---
        'getpass', 'platform', 'webbrowser',
        'ctypes', 'ctypes.util',
        'locale', 'codecs', 'encodings', 'unicodedata',
        'importlib.metadata', 'pkgutil', 'zipimport', 'runpy',
        'site', 'sysconfig', 'inspect',
        # --- Core runtime (cheap insurance; several are dynamic
        # somewhere in the three packages' long tail) ---
        'os', 'sys', 're', 'io', 'time', 'datetime', 'calendar',
        'threading', 'queue', 'contextlib', 'warnings',
        'string', 'textwrap', 'copy', 'pprint', 'difflib',
        'glob', 'fnmatch', 'tempfile', 'pathlib', 'shutil', 'stat',
        'subprocess', 'signal', 'traceback', 'random', 'math',
        'collections', 'collections.abc', 'functools', 'itertools',
        'operator', 'typing', 'enum', 'dataclasses', 'weakref',
        'types', 'heapq', 'bisect',
        'concurrent', 'concurrent.futures',
        # Third-party modules we are now bundling
        'requests', 'flask',
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
