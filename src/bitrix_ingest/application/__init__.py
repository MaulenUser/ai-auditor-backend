"""Application layer — use-case services and the ports they depend on.

The layer defines Protocols (``ports.py``) for the capabilities it needs
(Bitrix access, JSON output) and orchestrates them to deliver a concrete
export. It knows nothing about ``requests`` or the filesystem.
"""
