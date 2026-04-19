"""Infrastructure layer — concrete implementations (HTTP, filesystem, logging).

Everything here is a detail that can be swapped out. Higher layers depend on
the ports declared under ``application.ports``, not on these modules directly.
"""
