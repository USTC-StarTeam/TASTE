"""Namespace bridge for TASTE packages split across framework, web, and stage modules."""
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
