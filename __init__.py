"""Hermes plugin entry point."""
import threading

from .butler.awareness import register_awareness_and_controls
from .butler.event_hooks import register_event_hooks
from .butler.expressions import register_expression_tools
from .butler.imessage_tools import register_imessage_tools
from .butler.runner import Runner, get_active_runner, set_active_runner
from .butler.tools import register_tools

__all__ = ['register', 'get_active_runner']


def register(ctx):
    state = {}
    lock = threading.Lock()

    # Resolve profile-scoped storage lazily inside an admitted gateway turn or
    # authenticated bound local/TUI/desktop turn, never at registration time.
    def start(**kwargs):
        with lock:
            if state.get('closed'):
                return
            if 'runner' not in state:
                from plugins.plugin_storage import plugin_data_dir
                from .butler.store import ResponsibilityStore
                prof_dir = plugin_data_dir('butler')
                path = prof_dir / 'responsibilities.sqlite3'
                state['runner'] = Runner(
                    ctx,
                    lambda: ResponsibilityStore(path),
                    directory=prof_dir,
                )
            runner = state['runner']
            set_active_runner(runner)
            runner.start()

    def close():
        with lock:
            state['closed'] = True
            runner = state.get('runner')
        if runner:
            runner.close()

    register_tools(ctx)
    register_expression_tools(ctx)
    register_imessage_tools(ctx)
    register_event_hooks(ctx)
    register_awareness_and_controls(ctx, on_bound_turn=start)
    ctx.register_hook('post_gateway_admission', start)
    ctx.on_unload(close)
