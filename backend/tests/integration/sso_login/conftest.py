import os
import sys
import types

if os.name == "nt" and "fcntl" not in sys.modules:
    module = types.ModuleType("fcntl")
    module.LOCK_EX, module.LOCK_SH, module.LOCK_UN, module.LOCK_NB = 2, 1, 8, 4

    def unsupported_lock(*_args, **_kwargs):
        raise RuntimeError("POSIX locking is outside Windows auth integration tests")

    module.flock = unsupported_lock
    sys.modules["fcntl"] = module
