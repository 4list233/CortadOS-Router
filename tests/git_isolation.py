"""Keep the machine's git configuration out of tests that build real repositories (#119).

GIT_CONFIG_GLOBAL / GIT_CONFIG_NOSYSTEM alone are not enough: git still reads its default excludes and attributes
files from $XDG_CONFIG_HOME/git (or ~/.config/git) whatever the config says, and inherited GIT_* variables (config
injected with GIT_CONFIG_PARAMETERS / GIT_CONFIG_COUNT by a parent `git -c`, GIT_DIR or GIT_INDEX_FILE from a hook
running the suite) override the test's own repositories. Either can make a throwaway worktree look dirty or ignored
on one machine and clean on another."""
import os
import tempfile
import unittest

# set here; every other inherited GIT_* variable is removed for the test
OWN = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT")


def isolate_git(case: unittest.TestCase) -> dict:
    """Patch os.environ for the duration of `case` so every git it starts (the test's own and the module's) sees no
    user, system, XDG or inherited configuration. Returns the variables set."""
    xdg = tempfile.mkdtemp(prefix="git-xdg-")              # empty: no git/config, git/ignore or git/attributes
    case.addCleanup(lambda: os.path.isdir(xdg) and os.rmdir(xdg))
    saved = dict(os.environ)

    def restore():
        os.environ.clear()
        os.environ.update(saved)

    case.addCleanup(restore)
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        del os.environ[k]
    own = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "XDG_CONFIG_HOME": xdg}
    os.environ.update(own)
    return own
