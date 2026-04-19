"""Bitrix ingest package.

The package is organised by architectural layer:

- :mod:`bitrix_ingest.domain`         - pure entities and domain rules
- :mod:`bitrix_ingest.application`    - use-case orchestration and ports
- :mod:`bitrix_ingest.infrastructure` - HTTP, persistence, logging adapters
- :mod:`bitrix_ingest.cli`            - command-line entry points
"""
