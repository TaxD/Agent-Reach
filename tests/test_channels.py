# -*- coding: utf-8 -*-
"""Tests for channel registry basics and URL routing."""

from agent_reach.channels import get_all_channels, get_channel


class TestChannelRegistry:
    def test_get_channel_by_name(self):
        ch = get_channel("github")
        assert ch is not None
        assert ch.name == "github"

    def test_get_unknown_channel_returns_none(self):
        assert get_channel("not-exists") is None

    def test_all_channels_registered(self):
        names = [ch.name for ch in get_all_channels()]
        for name in ("web", "github", "twitter", "reddit", "youtube", "hackernews", "stackoverflow",
                     "lobsters", "devto", "mdn", "github-trending", "npm", "producthunt", "medium",
                     "dribbble", "bluesky", "rss", "geeknews", "yozm"):
            assert name in names
        assert len(names) == len(set(names))

    def test_every_channel_tells_the_agent_when_to_use_it(self):
        for ch in get_all_channels():
            assert ch.description and len(ch.purpose) > 20, ch.name


class TestRedditChannel:
    def test_can_handle_reddit_urls(self):
        from agent_reach.channels.reddit import RedditChannel
        ch = RedditChannel()
        assert ch.can_handle("https://www.reddit.com/r/python/comments/abc123/")
        assert ch.can_handle("https://redd.it/abc123")
        assert not ch.can_handle("https://github.com/user/repo")
        assert not ch.can_handle("https://news.ycombinator.com/item?id=1")
