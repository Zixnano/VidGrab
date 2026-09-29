Video Grabber v5, batch 1
All files are complete replacements (not diffs). Compile-checked only; not run.

settings.py    Adds DISPLAY_VERSION (reads install-version.txt; falls back to
               APP_VERSION). APP_VERSION itself is unchanged.
engines.py     yt_dlp imported lazily inside probe() and download(). Playlists
               save to <playlist name>/NNN - title.ext, merge to mp4, sort ties
               toward avc1/m4a (resolution still first). probe() accepts
               forwarded headers. Unknown vcodec + WxH resolution no longer
               sorts as audio-only. Fixed two invalid-escape docstrings.
api.py         yt_dlp imported lazily in /probe. /probe-formats forwards
               Cookie/Referer/User-Agent (strings only, CR/LF stripped, cookie
               cap 32768) and logs header names, lengths and an 8-char cookie
               prefix. Error text no longer reads "None". /jobs no longer
               returns each job's cookie.
gui_qt.py      Title/status/About use DISPLAY_VERSION. DownloadModel.update_items
               does incremental dataChanged (changed columns only, full reset
               when the row set changes). Only done rows are draggable. New
               "Copy file" context item (file goes on the clipboard). Progress
               bar animation cache keyed by job id. Context menu re-looks-up
               the row after exec. load_settings() cached by st_mtime_ns
               (callers must not mutate the result). Dialog fade plays only
               for the first dialog of the session.
content.js     pageUrlFor(): sends the real tweet URL instead of x.com/home,
               shows "Couldn't find tweet link" on failure. "audio only" label
               fixed for silent Twitter MP4s. Probe passes the page URL.
background.js  PROBE_FORMATS forwards cookies (url origin first, page as
               fallback), referer and user agent to /probe-formats.

Not touched: jobs.py.
Apply api.py + content.js + background.js together (probe protocol changed).
