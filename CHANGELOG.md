# Changelog

## 1.0.0 - 2026-11-06

First release.

- `AltGenerator` and `AsyncAltGenerator` (asyncio and trio): generate alt texts from bytes, files, uploads
  (FastAPI/Starlette, Django, Werkzeug) and Pillow images; usage; the connection check; sending edited alts
  back to the account; batches that wait out the per-minute cap and stop at the plan's limit.
- Images are identified from their content, checked before decoding and rebuilt from their pixels with Pillow:
  no metadata leaves the server. Paths are read only inside named directories.
- `ConnectionClient` and `AsyncConnectionClient`: connect, verify, disconnect, the same handshake as the CMS plugins.
- `dockaccess-alt` CLI: `connect`, `status`, `generate` (directories, a resumable manifest, JSON Lines, exit
  codes for pipelines), `check`, `disconnect`.
- FastAPI (lifespan, dependency, exception handler), Django (settings, validator with Polish messages) and
  Flask (extension, error handler) helpers, and receivers for signed alt pushes.
- `dockaccess_alt.testing.FakeAltApi` for applications' tests.
