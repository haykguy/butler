"""Hermes plugin entry point."""
import threading

from .overseer.awareness import register_awareness_and_controls
from .overseer.tools import register_tools
from .overseer.expressions import register_expression_tools
from .overseer.runner import Runner
from .overseer.event_hooks import register_event_hooks


def register(ctx):
    register_tools(ctx)
    register_expression_tools(ctx)
    register_event_hooks(ctx)
    register_awareness_and_controls(ctx)
    # Resolve profile-scoped storage in the admitted gateway callback, not in a
    # worker thread or at registration (which also occurs in non-gateway hosts).
    state = {}
    lock = threading.Lock()
    def start(**kwargs):
        with lock:
            if state.get('closed'):
                return
            if 'runner' not in state:
                from plugins.plugin_storage import plugin_data_dir
                from .overseer.store import ResponsibilityStore
                path = plugin_data_dir('overseer') / 'responsibilities.sqlite3'
                state['runner'] = Runner(ctx, lambda: ResponsibilityStore(path))
            state['runner'].start()
    def close():
        with lock:
            state['closed'] = True
            runner = state.get('runner')
        if runner:
            runner.close()
    ctx.register_hook('post_gateway_admission', start)
    ctx.on_unload(close)
