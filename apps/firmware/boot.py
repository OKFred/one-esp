"""Start diagnostics with bounded allocation pressure before module imports."""

import gc

_original_gc_threshold = None
try:
    _original_gc_threshold = gc.threshold()
    gc.threshold(8192)
except Exception:
    pass

try:
    from application.runtime import run
    run()
finally:
    if _original_gc_threshold is not None:
        try:
            gc.threshold(_original_gc_threshold)
        except Exception:
            pass
