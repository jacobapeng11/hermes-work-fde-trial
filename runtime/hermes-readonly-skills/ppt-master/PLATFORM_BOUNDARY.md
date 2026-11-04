# Platform Boundary Notes (Hermes Platform Edition)

This file is added by the Hermes platform distribution. It is not part of the
upstream ppt-master release and may be replaced freely by later platform
builds.

The platform Runtime has no outbound AI service credentials. The following
upstream capabilities are intentionally not shipped in this image:

- AI image generation (`scripts/image_gen.py`, `scripts/image_backends/`)
- Image search (`scripts/image_search.py`, `scripts/image_sources/`)
- Gemini watermark removal (`scripts/gemini_watermark_remover.py`)
- Narration audio / TTS (`scripts/notes_to_audio.py`, `scripts/tts_backends/`)
- Narrated video export (`scripts/powerpoint_video.py`)

When a workflow step references one of the removed capabilities, skip that
step and continue with the offline pipeline (text, native geometry, charts,
embedded local assets). Do not ask the user for external API keys and do not
attempt network workarounds.

Additional platform reductions (dependencies intentionally not installed):

- Local web UI tools (`scripts/confirm_ui/`, `scripts/spec_review/`,
  `scripts/svg_editor/`): no Flask/Werkzeug in the Runtime.
- `ipynb`/`epub` source conversion: no nbconvert/nbformat/ebooklib.
- TLS-fingerprint web fetching (e.g. WeChat articles): no curl_cffi; plain
  requests remain available.
- Python Playwright visual review / SVG picture extraction: not installed.
- Video subtitles: stable-whisper not installed (narrated video already
  removed).
- SVG→PNG rendering uses the pure-Python svglib fallback instead of CairoSVG.

All core routes (generate, beautify, edit native PPTX, template creation) and
the PDF/DOCX/XLSX/HTML source conversion paths are fully offline and
available. Never run `pip install` inside a session; missing optional
dependencies mean that specific sub-capability is unavailable.
