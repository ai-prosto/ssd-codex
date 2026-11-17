from __future__ import annotations

import socket

import pytest


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything tries to open a network connection."""

    def guard(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted during a pure parsing step")

    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
