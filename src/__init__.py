__all__ = ["FPLScout"]


def __getattr__(name):
    """Avoid importing API-only dependencies when using training utilities."""
    if name == "FPLScout":
        from .scout import FPLScout

        return FPLScout
    raise AttributeError(name)
