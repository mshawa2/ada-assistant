# Makes `tests` a real package so pytest imports conftest.py exactly once, as
# `tests.conftest`. Without this it gets imported twice — once as top-level
# `conftest` for the plugin hooks, once as `tests.conftest` for the shared
# fakes — and the two copies each build their own temp data directory, so the
# cleanup fixture ends up wiping a different directory than the one the code
# under test actually writes to.
