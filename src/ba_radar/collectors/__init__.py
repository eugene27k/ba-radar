"""Collector package.

Importing this package registers every collector. `html_diff` and
`github_commits_path` land in Stage 3; sources using them are inactive in the registry
until then, and `get_collector` returning None is handled as a logged skip.
"""

from ba_radar.collectors import github_releases, hn_algolia, rss  # noqa: F401
from ba_radar.collectors.base import (
    Collector,
    FetchContext,
    FetchResult,
    get_collector,
    register,
    registered_methods,
)
from ba_radar.collectors.http import HttpFetcher, SourceUnavailable

__all__ = [
    "Collector",
    "FetchContext",
    "FetchResult",
    "HttpFetcher",
    "SourceUnavailable",
    "get_collector",
    "register",
    "registered_methods",
]
