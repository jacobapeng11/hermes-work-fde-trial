import os
import sys
import types

if os.name == "nt" and "fcntl" not in sys.modules:
    module = types.ModuleType("fcntl")
    module.LOCK_EX, module.LOCK_SH, module.LOCK_UN, module.LOCK_NB = 2, 1, 8, 4

    def unsupported_lock(*_args, **_kwargs):
        raise RuntimeError("POSIX file locking is outside the Windows login test boundary")

    module.flock = unsupported_lock
    sys.modules["fcntl"] = module
