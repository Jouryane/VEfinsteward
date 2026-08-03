"""
VE5 receiver package.

Keep package import lightweight for the desktop API. Heavy components such as
OCR, model clients, and file watchers should be imported from their concrete
modules only when a workflow actually needs them.
"""

__version__ = "0.1.0"

__all__ = [
    "config",
    "pipeline",
    "watcher",
    "asset_classifier",
    "financial_rag",
]
