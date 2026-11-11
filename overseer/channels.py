"""Capability-checked operation descriptions, not channel implementations.

A host adapter must discover actual capabilities and execute operations; merely
creating an operation does not send a message or modify a channel.
"""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ChannelCapabilities:
    reactions: bool = False
    effects: bool = False
    threads: bool = False


@dataclass(frozen=True)
class ChannelOperation:
    kind: Literal['reaction', 'effect', 'thread']
    target: str
    value: str


def prepare_operation(capabilities: ChannelCapabilities, kind: str,
                      target: str, value: str) -> ChannelOperation:
    """Return a requested operation only if the adapter declares support."""
    names = {'reaction': 'reactions', 'effect': 'effects', 'thread': 'threads'}
    if kind not in names:
        raise ValueError(f'unknown channel operation: {kind}')
    if not getattr(capabilities, names[kind]):
        raise ValueError(f'channel does not support {kind}')
    if not target or not value:
        raise ValueError('target and value are required')
    return ChannelOperation(kind, target, value)
