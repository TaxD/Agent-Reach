# -*- coding: utf-8 -*-
"""
Channel registry — every platform exposed by `agent-reach <channel> <action>`.
"""

from typing import List, Optional

from .base import Channel
from .bluesky import BlueskyChannel
from .devto import DevToChannel
from .feed_sites import (
    DribbbleChannel,
    GeekNewsChannel,
    MediumChannel,
    ProductHuntChannel,
    YozmChannel,
)
from .github import GitHubChannel
from .github_trending import GitHubTrendingChannel
from .hackernews import HackerNewsChannel
from .lobsters import LobstersChannel
from .mdn import MDNChannel
from .npm import NpmChannel
from .reddit import RedditChannel
from .rss import RSSChannel
from .stackoverflow import StackOverflowChannel
from .twitter import TwitterChannel
from .web import WebChannel
from .youtube import YouTubeChannel

ALL_CHANNELS: List[Channel] = [
    GitHubChannel(),
    GitHubTrendingChannel(),
    StackOverflowChannel(),
    MDNChannel(),
    NpmChannel(),
    HackerNewsChannel(),
    LobstersChannel(),
    DevToChannel(),
    GeekNewsChannel(),
    YozmChannel(),
    MediumChannel(),
    ProductHuntChannel(),
    DribbbleChannel(),
    YouTubeChannel(),
    BlueskyChannel(),
    RedditChannel(),
    TwitterChannel(),
    RSSChannel(),
    WebChannel(),
]


def get_channel(name: str) -> Optional[Channel]:
    """Get a channel by name."""
    for ch in ALL_CHANNELS:
        if ch.name == name:
            return ch
    return None


def get_all_channels() -> List[Channel]:
    """Get all registered channels."""
    return ALL_CHANNELS


__all__ = [
    "Channel",
    "ALL_CHANNELS",
    "get_channel",
    "get_all_channels",
]
