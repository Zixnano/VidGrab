# Architecture (v4.0.0)

- **server.py** — Flask entry, registers api routes, serves the GUI.
- **api.py** — HTTP routes; thin, delegates to jobs/downloader/settings.
- **jobs.py** — job registry, status state machine, snapshot/restore,
  multi-quality filename suffixes.
- **downloader.py** — worker pool; generic segmented/simple downloads,
  yt-dlp dispatch via engines, post-download conversion, `_job_dest`
  collision disambiguation.
- **engines.py** — YtDlpEngine / StreamlinkEngine with `route_for()`
  honoring `per_site_engine` / `preferred_engines` / `engines_disabled`.
- **settings.py** — persistent STATE, rules, categories, `_unique_path`.
- **palette.py / gui_style.py** — theme tokens, THEME_PRESETS,
  resolve_palette(preset → accent → token overrides), QSS builder.
- **gui_qt.py** — PySide6 UI; `_ApiFetchWorker` keeps HTTP off the GUI
  thread; animation helpers; Settings Theme tab.
- **extension/** — content.js pill (probe formats, download, 4-mode
  recorder engine, menu), background/popup/options pages.
- **backend/tests/** — pytest suite; conftest stubs heavy deps.
